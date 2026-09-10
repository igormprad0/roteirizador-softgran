from __future__ import annotations
import json
import re
import sqlite3
from pathlib import Path

from rapidfuzz import fuzz, process

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
_TIPO_VIA = {"RUA", "AVENIDA", "RODOVIA", "ALAMEDA", "TRAVESSA", "PRACA",
             "ESTRADA", "LARGO", "MARGINAL"}

# Diferença máxima (em número de porta) para confiar no vizinho conhecido
# mais próximo como posição. Quarteirões urbanos em Dourados avançam em
# dezenas por lote; um vizinho a mais de 200 números de distância pode estar
# em outro trecho da via e a extrapolação vira um chute. Acima disso, cai no
# meio do segmento escolhido em vez de arriscar uma posição inventada.
_NUMBER_MAX_GAP = 200


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
            result = (self._by_housenumber(con, n)
                      or self._by_street(con, n)
                      or self._by_bairro(con, n)
                      or self._by_cidade(con, n)
                      or GeoResult(0.0, 0.0, "failed", "none"))
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
        r = con.execute(
            "SELECT lon, lat FROM place WHERE kind = ? AND name_norm = ? LIMIT 1",
            (kind, name_norm)).fetchone()
        return (r["lon"], r["lat"]) if r else None

    @staticmethod
    def _dist2(a: tuple[float, float], b: tuple[float, float]) -> float:
        """Distância ao quadrado em graus. Só serve para ordenar candidatos —
        não converter para metros, a escala de lon/lat difere."""
        return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2

    def _by_housenumber(self, con, n: NormalizedAddress) -> GeoResult | None:
        """Casamento exato de número. Raro em Dourados (111 números no índice
        inteiro), mas quando acerta é a melhor posição que existe."""
        if not n.number or not n.street:
            return None
        r = con.execute(
            "SELECT lon, lat FROM housenumber WHERE street_norm = ? AND number = ?"
            " LIMIT 1", (n.street, n.number)).fetchone()
        if r is None:
            return None
        return GeoResult(r["lon"], r["lat"], "high", "street_exact", n.street, 100.0)

    def _candidates(self, con, n: NormalizedAddress) -> list[sqlite3.Row]:
        terms = [t for t in _FTS_SAFE.sub(" ", n.street).split()
                 if len(t) > 2 and t not in _TIPO_VIA]
        if not terms:
            return []
        query = " OR ".join(terms)
        ids = [row["street_id"] for row in con.execute(
            "SELECT street_id FROM street_fts WHERE street_fts MATCH ? LIMIT 400",
            (query,))]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return con.execute(
            f"SELECT * FROM street WHERE street_id IN ({marks})", ids).fetchall()

    def _by_street(self, con, n: NormalizedAddress) -> GeoResult | None:
        """Casa o nome da via, escolhe entre os segmentos homônimos pelo bairro
        (ou pela cidade), e só então posiciona o ponto dentro do segmento."""
        if not n.street:
            return None
        rows = self._candidates(con, n)
        if not rows:
            return None

        nomes = {r["name_norm"] for r in rows}
        best = process.extractOne(n.street, list(nomes), scorer=fuzz.WRatio)
        if best is None or best[1] < self._lo:
            return None
        name, score = best[0], float(best[1])

        segmentos = [r for r in rows if r["name_norm"] == name]
        # Âncora: bairro se o ERP informou e o índice conhece, senão a cidade.
        # `street.city_norm` NÃO serve — está vazio em 99,9% das linhas.
        ancora = (self._place(con, "bairro", n.bairro) if n.bairro else None) \
            or self._place(con, "cidade", n.city)
        row = self._pick_segment(segmentos, ancora)
        pts = json.loads(row["coords_json"])

        if score >= self._hi and n.street == name:
            confidence, source = "high", "street_exact"
        elif score >= self._hi:
            confidence, source = "medium", "street_fuzzy"
        elif n.number:
            confidence, source = "medium", "street_fuzzy"
        else:
            confidence, source = "medium", "street_mid"

        lon, lat = self._point_on_street(con, pts, name, n)
        return GeoResult(lon, lat, confidence, source, name, score)

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

    def _point_on_street(self, con, pts, name_norm: str,
                         n: NormalizedAddress) -> tuple[float, float]:
        """Com número e com vizinhos conhecidos: usa o número mais próximo.
        Sem dado de número: ponto médio DO SEGMENTO escolhido, não da via."""
        meio = tuple(pts[len(pts) // 2])
        if not n.number:
            return meio
        try:
            alvo = int(n.number)
        except ValueError:
            return meio

        vizinhos = con.execute(
            "SELECT number, lon, lat FROM housenumber WHERE street_norm = ?",
            (name_norm,)).fetchall()
        candidatos = []
        for v in vizinhos:
            try:
                candidatos.append((abs(int(v["number"]) - alvo), v["lon"], v["lat"]))
            except (TypeError, ValueError):
                continue
        if candidatos:
            gap, lon, lat = min(candidatos, key=lambda c: c[0])
            if gap <= _NUMBER_MAX_GAP:
                return (lon, lat)
        return meio

    def _by_bairro(self, con, n: NormalizedAddress) -> GeoResult | None:
        if not n.bairro:
            return None
        r = con.execute(
            "SELECT lon, lat FROM place WHERE kind = 'bairro' AND name_norm = ?"
            " LIMIT 1", (n.bairro,)).fetchone()
        if r is None:
            return None
        return GeoResult(r["lon"], r["lat"], "low", "bairro", n.bairro, 50.0)

    def _by_cidade(self, con, n: NormalizedAddress) -> GeoResult | None:
        r = con.execute(
            "SELECT lon, lat FROM place WHERE kind = 'cidade' AND name_norm = ? LIMIT 1",
            (n.city,)).fetchone()
        if r is None:
            return None
        return GeoResult(r["lon"], r["lat"], "low", "cidade", n.city, 25.0)
