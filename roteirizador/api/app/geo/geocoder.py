from __future__ import annotations
import dataclasses
import json
import re
import sqlite3
from pathlib import Path

from rapidfuzz import fuzz

from ..db.local import LocalStore
from ..models import Address, GeoResult
from .normalize import NormalizedAddress, normalize_address

_FTS_SAFE = re.compile(r"[^A-Z0-9 ]")

# Tipos de logradouro genéricos: quase toda via os tem, então usá-los como
# termo de busca FTS faz qualquer endereço "casar" com a primeira rua que
# contenha essa palavra (achado ao rodar: "RUA QUE NAO EXISTE EM LUGAR
# NENHUM" casava com "RUA MATO GROSSO" via WRatio=85 só por causa do "RUA"
# compartilhado). Continuam fazendo parte de `name_norm`/`n.street` para o
# score fuzzy — só não valem como termo de recuperação de candidatos.
#
# NÃO é tudo-ou-nada: "RUA 6", "RUA 8", "ALAMEDA 1".."9" e "RUA MARGINAL CD
# 01" são convenção corrente de loteamento brasileiro e, depois de tirar
# essas palavras, o conjunto de termos fica vazio (ou perde o único token que
# sobrou, ex. "MARGINAL" está na própria lista). Quando isso acontece,
# `_candidates` cai de volta para os termos originais (sem o filtro) em vez
# de devolver zero candidatos.
_TIPO_VIA = {"RUA", "AVENIDA", "RODOVIA", "ALAMEDA", "TRAVESSA", "PRACA",
             "ESTRADA", "LARGO", "MARGINAL"}

# Conectivos gramaticais: nao distinguem rua nenhuma, so ocupam vaga no
# LIMIT da busca FTS. Achado real: "ALAMEDA DAS HORTENCIAS" tem 7 segmentos
# no indice (3 perto de Dourados), mas a busca "ALAMENDA OR DAS OR
# HORTENCIAS" gastava o LIMIT 400 inteiro em ruas que so compartilhavam a
# palavra "DAS" (frequentissima em nomes de logradouro) e devolvia UM UNICO
# segmento -- que por acaso era o distante. "DE/DA/DO/E" ja saiam pelo
# filtro de tamanho (`len(t) > 2`); ficam aqui mesmo assim para o filtro
# nao depender silenciosamente desse efeito colateral.
_CONECTIVOS = {"DAS", "DOS", "DE", "DA", "DO", "E"}

# Diferença máxima (em número de porta) para confiar no vizinho conhecido
# mais próximo como posição, ao interpolar dentro de UMA MESMA cidade.
# Quarteirões urbanos avançam em dezenas por lote; um vizinho a mais de 200
# números pode estar num trecho distante da mesma via (a Marcelino Pires,
# por exemplo, soma 9,3 km em segmentos separados) e a extrapolação vira um
# chute. Isto é ortogonal ao escopo de cidade abaixo: mesmo depois de garantir
# que o vizinho está na cidade certa, ele pode estar no lado errado da cidade.
_NUMBER_MAX_GAP = 200

# Raio (em graus, não metros — a escala de lon/lat difere) além do qual um
# candidato é recusado por implausível para a cidade do endereço. ~0.30°
# nesta latitude cobre um município com folga e recusa a cidade vizinha.
# Existe porque, sem ele, quatro consultas (housenumber exato, vizinho mais
# próximo para interpolação, bairro e a âncora de bairro usada para escolher
# segmento de via) varriam o extrato inteiro sem nenhuma discriminação
# geográfica — o achado real foi "RUA MATO GROSSO, 1973" em Dourados
# resolvendo a 145 km, e um bairro "CENTRO" (existe em 10 municípios do
# extrato) puxando o centroide de outra cidade para dentro do cálculo de
# segmento.
CITY_RADIUS_DEG = 0.30

# Prefixos quase decorativos que ERP e OSM discordam em incluir ou não no
# nome de um bairro ("JARDIM AGUA BOA" vs "AGUA BOA"). `_bairro_variantes`
# tenta o nome com e sem cada um destes antes de desistir.
_BAIRRO_PREFIXOS = ("JARDIM", "VILA", "PARQUE", "CONJUNTO", "RESIDENCIAL")


class Geocoder:
    def __init__(self, index_db: Path, store: LocalStore,
                 fuzzy_high: int = 88, fuzzy_low: int = 75):
        self._db = Path(index_db)
        self._store = store
        self._hi, self._lo = fuzzy_high, fuzzy_low

    # ------------------------------------------------------------------
    def geocode(self, addr: Address, default_city: str = "DOURADOS",
                default_uf: str = "MS") -> tuple[str, GeoResult]:
        n = normalize_address(addr, default_city, default_uf)

        cached = self._store.get_geocode(n.key)
        if cached is not None:
            return n.key, cached

        con = sqlite3.connect(self._db)
        con.row_factory = sqlite3.Row
        try:
            # Resolvido uma única vez por geocode() e repassado a toda
            # consulta que precise de escopo geográfico — inclusive à que
            # alimenta o cascata de bairro/segmento abaixo.
            city_centro = self._place(con, "cidade", n.city)
            result = (self._by_housenumber(con, n, city_centro)
                      or self._by_street(con, n, city_centro)
                      or self._by_bairro(con, n, city_centro)
                      or self._by_cidade(con, n)
                      or GeoResult(0.0, 0.0, "failed", "none"))
            if city_centro is None and result.confidence in ("high", "medium"):
                # Sem centroide da cidade, TODO o escopo geográfico some em
                # silêncio: `_partition_by_proximity` devolve tudo como
                # "perto" e a rede de segurança do fim de `_by_street` não
                # tem contra o que medir. Um casamento em qualquer ponto do
                # extrato volta como high/medium -- caso real: cidade
                # "PEROLA D'OESTE" (Paraná, fora do extrato) resolvendo em
                # "CORONEL PONCIANO", em Dourados, com `medium`. O resultado
                # continua sendo o melhor palpite disponível, mas a
                # confiança não pode afirmar posição: teto em `low`.
                result = dataclasses.replace(result, confidence="low")
        finally:
            con.close()

        if result.confidence != "failed":
            self._store.put_geocode(n.key, result)
        return n.key, result

    def pin(self, address_key: str, lon: float, lat: float) -> GeoResult:
        return self._store.pin_geocode(address_key, lon, lat)

    # ------------------------------------------------------------------
    # -- âncoras geográficas -------------------------------------------
    def _place(self, con, kind: str, name_norm: str) -> tuple[float, float] | None:
        """Busca sem escopo — só serve para resolver a própria cidade, que
        não tem contra o que se desambiguar. Nomes de bairro usam
        `_place_scoped`, que existe exatamente porque isto sozinho não
        discrimina município (`place.city_norm` para bairro está preenchido
        em 1 de 1.388 linhas — inútil)."""
        r = con.execute(
            "SELECT lon, lat FROM place WHERE kind = ? AND name_norm = ? LIMIT 1",
            (kind, name_norm)).fetchone()
        return (r["lon"], r["lat"]) if r else None

    def _place_scoped(self, con, kind: str, name_norm: str,
                       city_centro: tuple[float, float] | None
                       ) -> tuple[float, float] | None:
        """Como `_place`, mas descarta candidatos a mais de `CITY_RADIUS_DEG`
        do centroide da cidade e fica com o mais próximo entre os que
        sobram. Necessário para bairro: mesmo nome existe em vários
        municípios do extrato (`CENTRO` em 10) e não há `city_norm`
        utilizável para filtrar antes.

        Para `kind == "bairro"`, também tolera prefixo ausente/a mais
        (`JARDIM`, `VILA`, `PARQUE`, `CONJUNTO`, `RESIDENCIAL`) — ERP e OSM
        discordam sobre isso o tempo todo (`JARDIM AGUA BOA` vs `AGUA BOA`).
        O portão geográfico abaixo continua aplicado a QUALQUER variante que
        bater, então uma coincidência de nome (com ou sem prefixo) em outro
        município continua sendo recusada — a tolerância de nome não abre
        uma segunda porta para a colisão de cidade que o resto desta função
        já fecha."""
        nomes = self._bairro_variantes(name_norm) if kind == "bairro" else [name_norm]
        marks = ",".join("?" * len(nomes))
        rows = con.execute(
            f"SELECT lon, lat FROM place WHERE kind = ? AND name_norm IN ({marks})",
            (kind, *nomes)).fetchall()
        if not rows:
            return None
        if city_centro is None:
            r = rows[0]
            return (r["lon"], r["lat"])
        candidatos = [(r["lon"], r["lat"]) for r in rows
                      if self._dist2((r["lon"], r["lat"]), city_centro)
                      <= CITY_RADIUS_DEG ** 2]
        if not candidatos:
            return None
        return min(candidatos, key=lambda p: self._dist2(p, city_centro))

    @staticmethod
    def _bairro_variantes(name_norm: str) -> list[str]:
        """Gera as grafias plausíveis de um bairro sem inventar nomes: o que
        veio do ERP, sem um prefixo que ele já tenha, e com cada prefixo
        comum na frente quando ele não tem nenhum."""
        variantes = [name_norm]
        tinha_prefixo = False
        for pfx in _BAIRRO_PREFIXOS:
            if name_norm.startswith(pfx + " "):
                tinha_prefixo = True
                sem_prefixo = name_norm[len(pfx) + 1:]
                if sem_prefixo:
                    variantes.append(sem_prefixo)
        if not tinha_prefixo:
            variantes.extend(f"{pfx} {name_norm}" for pfx in _BAIRRO_PREFIXOS)
        return variantes

    @staticmethod
    def _dist2(a: tuple[float, float], b: tuple[float, float]) -> float:
        """Distância ao quadrado em graus. Só serve para ordenar candidatos —
        não converter para metros, a escala de lon/lat difere."""
        return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2

    def _by_housenumber(self, con, n: NormalizedAddress,
                        city_centro: tuple[float, float] | None) -> GeoResult | None:
        """Casamento exato de número. Raro em Dourados (111 números no índice
        inteiro), mas quando acerta é a melhor posição que existe.

        `housenumber.city_norm` está 64% preenchido (1.299 de 2.027) — ao
        contrário do de `street`, é utilizável. Tenta cidade exata primeiro;
        só recorre ao critério geométrico (candidato mais próximo do
        centroide, dentro de `CITY_RADIUS_DEG`) quando não há linha com
        `city_norm` batendo — o que cobre tanto os 36% sem `city_norm`
        quanto o caso de o ERP citar uma cidade que o índice grafa diferente.
        """
        if not n.number or not n.street:
            return None
        exato = con.execute(
            "SELECT lon, lat FROM housenumber"
            " WHERE street_norm = ? AND number = ? AND city_norm = ? LIMIT 1",
            (n.street, n.number, n.city)).fetchone()
        if exato is not None:
            return GeoResult(exato["lon"], exato["lat"], "high", "street_exact",
                             n.street, 100.0)
        if city_centro is None:
            return None
        candidatos = [
            (r["lon"], r["lat"]) for r in con.execute(
                "SELECT lon, lat FROM housenumber WHERE street_norm = ? AND number = ?",
                (n.street, n.number))
            if self._dist2((r["lon"], r["lat"]), city_centro) <= CITY_RADIUS_DEG ** 2
        ]
        if not candidatos:
            return None
        lon, lat = min(candidatos, key=lambda p: self._dist2(p, city_centro))
        return GeoResult(lon, lat, "high", "street_exact", n.street, 100.0)

    def _candidates(self, con, n: NormalizedAddress) -> list[sqlite3.Row]:
        tokens = [t for t in _FTS_SAFE.sub(" ", n.street).split() if len(t) > 2]
        terms = [t for t in tokens if t not in _TIPO_VIA and t not in _CONECTIVOS]
        if not terms:
            terms = tokens
        if not terms:
            return []
        query = " OR ".join(terms)
        # ORDER BY rank: sem isso, quais 400 linhas voltam para uma busca
        # que bate em centenas ("SANTOS"/"SOUZA" sozinhos batem 656/904 no
        # índice inteiro) depende da ordem incidental de armazenamento do
        # SQLite -- não garantida estável entre processos. `rank` é o bm25
        # embutido do FTS5: função pura do texto, mesma entrada sempre dá a
        # mesma ordem, em qualquer processo.
        ids = [row["street_id"] for row in con.execute(
            "SELECT street_id FROM street_fts WHERE street_fts MATCH ?"
            " ORDER BY rank LIMIT 400",
            (query,))]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return con.execute(
            f"SELECT * FROM street WHERE street_id IN ({marks})", ids).fetchall()

    def _by_street(self, con, n: NormalizedAddress,
                   city_centro: tuple[float, float] | None) -> GeoResult | None:
        """Casa o nome da via, escolhe entre os segmentos homônimos pelo bairro
        (ou pela cidade), e só então posiciona o ponto dentro do segmento.

        A pontuação fuzzy acontece SEPARADAMENTE por partição geográfica —
        primeiro entre os candidatos perto da cidade, e só se nenhum bater
        aí é que os distantes entram na disputa. Antes disso, um nome
        errado mas coincidentemente melhor pontuado (`"AVENIDA ARISTIDES
        CRISOSTOMO DOS SANTOS"`, 85,5, batendo `"RUA SANTOS DUMONT"`, 82,3)
        ou um empate exato desfeito por ordem arbitrária de iteração
        (`"RUA DOS SOUZA"` empatado 85,5 com `"RUA VEREADOR AGUIAR FERREIRA
        DE SOUZA"`, a rua certa, perto) vencia a rua certa que existia bem
        mais perto — o score global não sabia de geografia. Particionar
        antes de pontuar resolve isso na raiz, não com um portão no fim.
        """
        if not n.street:
            return None
        rows = self._candidates(con, n)
        if not rows:
            return None

        perto, longe = self._partition_by_proximity(rows, city_centro)

        escolha = self._best_name(n.street, perto)
        pool, distante = perto, False
        if escolha is None or escolha[1] < self._lo:
            # Nada perto bateu o suficiente -- só agora os distantes contam,
            # e só como último recurso (existe, mas não com confiança de
            # posição). Sem isto, uma rua real só teria "cidade"/"bairro"
            # como resposta, quando "existe, mas longe" é mais honesto.
            escolha = self._best_name(n.street, longe)
            pool, distante = longe, True
            if escolha is None or escolha[1] < self._lo:
                return None

        name, score = escolha
        segmentos = [r for r in pool if r["name_norm"] == name]
        # Âncora: bairro se o ERP informou e o índice conhece (já escopado
        # por cidade via `_place_scoped` — precisa ser resolvido ANTES de
        # virar âncora, senão um "CENTRO" de outro município arrasta a
        # escolha de segmento inteira para lá), senão o centroide da cidade.
        # `street.city_norm` NÃO serve — está vazio em 99,9% das linhas.
        ancora = (self._place_scoped(con, "bairro", n.bairro, city_centro)
                 if n.bairro else None) or city_centro
        row = self._pick_segment(segmentos, ancora)
        pts = json.loads(row["coords_json"])

        if distante:
            # Existe, mas nenhum segmento está perto da cidade do endereço
            # -- a mesma incerteza de "bairro"/"cidade", não de um match
            # local. Nunca high/medium, senão volta a contar como acerto
            # confiante um resultado que pode estar a centenas de km.
            confidence, source = "low", "street_fuzzy"
        elif score >= self._hi and n.street == name:
            confidence, source = "high", "street_exact"
        elif score >= self._hi:
            confidence, source = "medium", "street_fuzzy"
        elif n.number:
            confidence, source = "medium", "street_fuzzy"
        else:
            # Nem o nome da via é confiável (score < fuzzy_high) nem há
            # número para interpolar — o degrau mais fraco dos três "medium"
            # desta função, não um distinto/mais forte. Rebaixado para
            # "low": contar isto junto dos outros dois inflaria o
            # high+medium com o pior palpite da cascata.
            confidence, source = "low", "street_mid"

        lon, lat = self._point_on_street(con, pts, name, n, city_centro)

        if not distante and city_centro is not None and \
                self._dist2((lon, lat), city_centro) > CITY_RADIUS_DEG ** 2:
            # Rede de segurança: com a partição acima isto não deveria mais
            # disparar para o ramo "perto" (o segmento já veio filtrado),
            # mas continua barato e continua certo de se manter.
            return None

        return GeoResult(lon, lat, confidence, source, name, score)

    def _partition_by_proximity(self, rows: list[sqlite3.Row],
                                city_centro: tuple[float, float] | None
                                ) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        """Separa os segmentos candidatos em perto/longe do centroide da
        cidade ANTES de qualquer pontuação fuzzy -- é isto que faz a rua
        certa perto vencer a rua errada só coincidentemente bem pontuada.
        Sem cidade conhecida não há como separar; tudo vira "perto" (resta
        só o score mesmo, como antes desta mudança)."""
        if city_centro is None:
            return rows, []
        perto, longe = [], []
        for r in rows:
            centro = ((r["min_lon"] + r["max_lon"]) / 2,
                      (r["min_lat"] + r["max_lat"]) / 2)
            if self._dist2(centro, city_centro) <= CITY_RADIUS_DEG ** 2:
                perto.append(r)
            else:
                longe.append(r)
        return perto, longe

    @staticmethod
    def _best_name(street: str, rows: list[sqlite3.Row]
                   ) -> tuple[str, float] | None:
        """Pontua cada nome distinto e devolve o de maior score, com
        desempate DETERMINÍSTICO em caso de empate exato.

        `process.extractOne` sobre um `set` não serve: a ordem de iteração
        de um `set` de strings varia com o hash aleatório do processo
        (`PYTHONHASHSEED`), então dois scores empatados (achado real:
        `"RUA PORTO BELO"` e `"RUA PORTO IGUAÇU"`, ambos 90,0, ambos perto
        de Dourados) podiam escolher um ou outro dependendo só de qual
        processo rodou a busca — o mesmo endereço geocodificando diferente
        entre execuções. Aqui a lista é ordenada antes de pontuar (a ordem
        de entrada nunca decide nada) e o desempate é por nome mais curto
        — o casamento mais "justo" para o mesmo score, sem texto extra por
        trás — e só como último critério, ordem alfabética (não é escolha
        de mérito, é só o que garante uma única saída possível)."""
        if not rows:
            return None
        nomes = sorted({r["name_norm"] for r in rows})
        pontuados = [(nome, float(fuzz.WRatio(street, nome))) for nome in nomes]
        melhor_score = max(score for _, score in pontuados)
        empatados = [nome for nome, score in pontuados if score == melhor_score]
        escolhido = min(empatados, key=lambda nome: (len(nome), nome))
        return escolhido, melhor_score

    @staticmethod
    def _pick_segment(segmentos: list, ancora: tuple[float, float] | None):
        """Uma via é várias linhas de `street`. A Marcelino Pires são 20
        segmentos somando 9,3 km — escolher o errado erra por quilômetros."""
        if len(segmentos) == 1 or ancora is None:
            return segmentos[0]

        def centro(r):
            return ((r["min_lon"] + r["max_lon"]) / 2,
                    (r["min_lat"] + r["max_lat"]) / 2)
        return min(segmentos, key=lambda r: Geocoder._dist2(centro(r), ancora))

    def _point_on_street(self, con, pts, name_norm: str, n: NormalizedAddress,
                         city_centro: tuple[float, float] | None
                         ) -> tuple[float, float]:
        """Com número e com vizinhos conhecidos: usa o número mais próximo,
        restrito a vizinhos plausíveis para a cidade do endereço (mesma
        lógica de `_by_housenumber`: `city_norm` exato primeiro, geometria
        como reserva) e dentro de `_NUMBER_MAX_GAP`. Sem dado de número ou
        sem vizinho que passe nos dois filtros: ponto médio DO SEGMENTO
        escolhido, não da via."""
        meio = tuple(pts[len(pts) // 2])
        if not n.number:
            return meio
        try:
            alvo = int(n.number)
        except ValueError:
            return meio

        vizinhos = con.execute(
            "SELECT number, lon, lat, city_norm FROM housenumber WHERE street_norm = ?",
            (name_norm,)).fetchall()
        candidatos = []
        for v in vizinhos:
            try:
                gap = abs(int(v["number"]) - alvo)
            except (TypeError, ValueError):
                continue
            if gap > _NUMBER_MAX_GAP:
                continue
            mesma_cidade = v["city_norm"] and v["city_norm"] == n.city
            if not mesma_cidade:
                if city_centro is None:
                    continue
                if self._dist2((v["lon"], v["lat"]), city_centro) > CITY_RADIUS_DEG ** 2:
                    continue
            candidatos.append((gap, v["lon"], v["lat"]))
        if candidatos:
            _, lon, lat = min(candidatos, key=lambda c: c[0])
            return (lon, lat)
        return meio

    def _by_bairro(self, con, n: NormalizedAddress,
                   city_centro: tuple[float, float] | None) -> GeoResult | None:
        if not n.bairro:
            return None
        pt = self._place_scoped(con, "bairro", n.bairro, city_centro)
        if pt is None:
            return None
        return GeoResult(pt[0], pt[1], "low", "bairro", n.bairro, 50.0)

    def _by_cidade(self, con, n: NormalizedAddress) -> GeoResult | None:
        r = con.execute(
            "SELECT lon, lat FROM place WHERE kind = 'cidade' AND name_norm = ? LIMIT 1",
            (n.city,)).fetchone()
        if r is None:
            return None
        return GeoResult(r["lon"], r["lat"], "low", "cidade", n.city, 25.0)
