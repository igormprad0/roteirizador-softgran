from __future__ import annotations
import hashlib
import re
import unicodedata
from dataclasses import dataclass

from ..models import Address

_ABBREV = {
    "R": "RUA", "R.": "RUA",
    "AV": "AVENIDA", "AV.": "AVENIDA", "AVN": "AVENIDA",
    "ROD": "RODOVIA", "ROD.": "RODOVIA",
    "AL": "ALAMEDA", "AL.": "ALAMEDA",
    "TRAV": "TRAVESSA", "TV": "TRAVESSA",
    "PC": "PRACA", "PRC": "PRACA",
    "EST": "ESTRADA", "ESTR": "ESTRADA",
    "LG": "LARGO", "MAR": "MARGINAL",
}

_CIDADE_ALIAS = {
    "DDOS": "DOURADOS", "DORUADOS": "DOURADOS", "DOURDOS": "DOURADOS",
    "CG": "CAMPO GRANDE", "PPORA": "PONTA PORA",
}

_UFS = {"MS", "MT", "GO", "SP", "PR", "DF", "RJ", "MG"}

# "Q 13 LT 06" | "LOTE 06 QUADRA 09" | "QD37" | "LT05"
_QUADRA = re.compile(
    r"\b(?:Q|QD|QUADRA)\s*\.?\s*\d+[A-Z]?\b|\b(?:L|LT|LOTE)\s*\.?\s*\d+[A-Z]?\b")
# "CASA 2" | "APTO 4" | "BLOCO B" | "FUNDOS" -- unidade dentro do lote.
# So e aplicado ao trecho da string a partir do primeiro digito (ver
# split_number); antes disso essas palavras podem ser nome de rua de
# verdade ("Rua Casa Forte", "Rua Bloco B").
_UNIDADE = re.compile(r"\b(?:CASA|APTO|BLOCO|FUNDOS)\b\.?\s*[A-Z0-9]{0,3}\b")
# "N/C" | "S/N" | "SN" no fim -> sem numero. A virgula antes e opcional: ERPs
# escrevem tanto "RUA X, S/N" quanto "RUA X S/N" (ver test_split_number_
# casos_adicionais_complemento_e_km, caso "RUA MARILIA S/N").
_SEM_NUMERO = re.compile(r",?\s*(?:N/C|S/N|SN)\s*$")
# PRIMEIRO numero autonomo da string encerra o nome da rua -- tudo depois
# dele e complemento/lixo, nunca nome (achado da revisao: "RUA ONOFRE
# PEREIRA DE MATOS,970 CENTRO, 970" so bate com "RUA ONOFRE PEREIRA DE
# MATOS" no indice se o "970 CENTRO" inteiro sai do nome, nao so o ultimo
# numero). Por isso NAO e ancorado no fim da string ($) -- e o primeiro
# candidato valido que vale, com re.search varrendo da esquerda.
# Separador opcional por virgula/"N"/"Nº"/espaco antes do numero, sufixo
# "-A" tolerado depois. Nota: strip_accents(NFKD) decompoe "º" (ordinal
# masculino) para uma letra "O" solta (nao e um combining mark, entao nao e
# removido) -- por isso "N" tambem aceita ser seguido de "O" aqui.
# (?<!KM) evita capturar o numero de uma marca rodoviaria ("KM 5", "KM 12")
# como se fosse numero de casa.
# (?!\s+DE\b) e o que protege nomes de rua com data/numero embutido --
# "RUA 13 DE MAIO 500", "25 DE MARCO 100", "AVENIDA 7 DE SETEMBRO 120" --
# de serem cortados no numero errado: um numero seguido de " DE " e parte
# do nome, nunca o numero de casa: continua a varredura ate achar o
# proximo candidato (o numero de casa de verdade, mais adiante). O \b logo
# apos o grupo de digitos e obrigatorio: sem ele, quando o lookahead nega
# o casamento, o quantificador guloso \d{1,6} pode retroceder para MENOS
# digitos so para escapar da negacao (ex.: "13" vira "1", deixando "3"
# pendurado) -- \b falha entre dois digitos, entao barra esse retrocesso
# e forca a busca a pular para o proximo candidato de verdade.
_NUMERO = re.compile(
    r"(?:,\s*|\s+N[Oº°.]?\s*|(?<!KM)\s+)(\d{1,6})\b(?!\s+DE\b)(?:\s*-\s*[A-Z0-9]+)?")

# Anotacao solta entre parenteses no final do campo -- fechada ou nao (o
# ERP as vezes trunca o campo no meio: "(fundos Ecov"). Nunca e nome de
# rua nem numero; e descartada antes de qualquer outro processamento.
_PAREN_FINAL = re.compile(r"\s*\([^)]*\)?\s*$")


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def normalize_city(raw: str | None, default_city: str = "DOURADOS",
                    default_uf: str = "MS") -> tuple[str, str]:
    if not raw or not raw.strip():
        return default_city, default_uf
    s = strip_accents(raw).upper().strip()
    uf = default_uf
    m = re.search(r"[-/\s]\s*([A-Z]{2})\s*$", s)
    if m and m.group(1) in _UFS:
        uf = m.group(1)
        s = s[: m.start()].strip()
    s = re.sub(r"\s+", " ", s).strip(" -/")
    return _CIDADE_ALIAS.get(s, s) or default_city, uf


_ABBREV_TOKEN = re.compile(r"^([A-Z]+)([.:])(.*)$")


def expand_abbreviations(s: str) -> str:
    parts = re.sub(r"\s+", " ", s).strip().split(" ")
    if not parts:
        return " ".join(parts)
    primeiro = parts[0].upper()
    if primeiro in _ABBREV:
        parts[0] = _ABBREV[primeiro]
    else:
        # Abreviação colada ao nome por ponto OU dois-pontos, sem espaço:
        # "AV.MARCELINO", "AV:PRESIDENTE" (":" no lugar de "."), "R.JOAO",
        # "ROD.BR-163". O primeiro token inteiro ("AV.MARCELINO") nunca bate
        # no dicionário, então sem isto a abreviação nunca expande --
        # achado real: "AV.MARCELINO PIRES" não expande para "AVENIDA
        # MARCELINO PIRES", e existe uma "RUA MARCELINO PIRES" genuína e
        # distinta no índice que vence por fuzzy score quando a avenida
        # fica sem a palavra completa.
        m = _ABBREV_TOKEN.match(primeiro)
        if m:
            prefixo, _sep, resto = m.groups()
            chave = prefixo + "."  # ":" e "." sao a mesma abreviacao aqui
            if chave in _ABBREV:
                parts[0] = _ABBREV[chave]
                if resto:
                    parts.insert(1, resto)
    return " ".join(parts)


def _limpar(s: str) -> str:
    """Colapsa espacos e series de virgulas (deixadas por tokens removidos)."""
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"(,\s*)+", ", ", s)
    return s.strip().strip(",").strip()


def split_number(s: str) -> tuple[str, str | None, str | None]:
    """Devolve (rua, numero, complemento). Aceita texto cru; normaliza internamente.

    CASA/APTO/BLOCO/FUNDOS so contam como complemento quando aparecem DEPOIS
    do primeiro digito da string -- nunca antes. Isso e o que distingue
    "RUA A 100 CASA 2" (CASA e complemento, 100 e o numero) de "RUA CASA
    FORTE 200" (CASA e parte do nome da rua -- existe de verdade em Recife;
    o mesmo vale para "RUA BLOCO B" sem nenhum digito). Sem essa restricao de
    posicao, qualquer rua cujo nome contenha uma dessas palavras teria o
    nome corrompido (achado da revisao). Quadra/lote continuam podendo
    aparecer antes OU depois do numero, pois o marcador ("Q13", "LT 06") e
    inconfundivel e nao colide com nomes de rua.

    O PRIMEIRO numero autonomo da string encerra o nome da rua -- tudo
    depois dele (bairro repetido, anotacao livre, o mesmo numero de novo)
    e descartado do nome, nunca faz parte dele. Numeros que sao parte do
    proprio nome da rua ("13 DE MAIO", "25 DE MARCO", "7 DE SETEMBRO") sao
    protegidos porque um numero seguido de " DE " nunca e o numero de casa
    -- ver `_NUMERO`.
    """
    s = re.sub(r"[\n\t]+", " ", s or "")
    s = expand_abbreviations(strip_accents(s).upper())
    s = _PAREN_FINAL.sub("", s)

    complement_parts: list[str] = []

    quadras = _QUADRA.findall(s)
    if quadras:
        complement_parts.extend(q.strip() for q in quadras)
        s = _QUADRA.sub(" ", s)
    s = _limpar(s)

    s = _SEM_NUMERO.sub("", s)
    s = _limpar(s)

    primeiro_digito = re.search(r"\d", s)
    corte = primeiro_digito.start() if primeiro_digito else len(s)
    cabeca, resto = s[:corte], s[corte:]

    unidades = _UNIDADE.findall(resto)
    if unidades:
        complement_parts.extend(u.strip() for u in unidades)
        resto = _UNIDADE.sub(" ", resto)
    s = _limpar(cabeca + resto)

    number = None
    m = _NUMERO.search(s)
    if m:
        number = m.group(1)
        s = _limpar(s[: m.start()])

    complement = ", ".join(complement_parts) if complement_parts else None
    return s, number, complement


# Um número de porta é um número de porta: dígitos, no máximo com um sufixo
# de uma letra ("1234", "1234-A", "1234 B"). O campo NUMERO do ERP guarda
# muito mais que isso -- "Q16/L9" (quadra 16, lote 9), "S/N", "Q03 - LT01A".
# Arrancar os dígitos com `re.sub(r"\D", "")` transformava "Q16/L9" em 169 e
# entregava isso à interpolação de número de porta como se fosse a porta
# 169 da rua: um ponto inventado com aparência de precisão. Quando o valor
# não é um número de porta, o certo é dizer que NÃO HÁ número -- a cascata
# do geocoder já sabe cair para o segmento/bairro, que é honesto.
_NUMERO_PORTA = re.compile(r"^(\d{1,6})\s*-?\s*([A-Z])?$")


def _numero_de_porta(valor: str) -> str | None:
    """Devolve só os dígitos quando o valor É um número de porta, senão None.
    O sufixo de letra é aceito na ENTRADA (para "1234-A" não ser descartado
    junto com "Q16/L9") mas não vai para a chave: o índice de números do OSM
    guarda só o numeral, e era assim que a versão anterior já se comportava
    para esse caso."""
    m = _NUMERO_PORTA.match(strip_accents(valor).upper().strip())
    return m.group(1) if m else None


@dataclass(frozen=True)
class NormalizedAddress:
    street: str
    number: str | None
    bairro: str | None
    city: str
    uf: str
    complement: str | None
    key: str


def normalize_address(addr: Address, default_city: str = "DOURADOS",
                       default_uf: str = "MS") -> NormalizedAddress:
    city, uf = normalize_city(addr.cidade, default_city, default_uf)
    if addr.uf and addr.uf.strip():
        uf = strip_accents(addr.uf).upper().strip()[:2] or uf

    street, number, complement = split_number(addr.logradouro or addr.raw or "")

    if addr.numero and str(addr.numero).strip():
        number = _numero_de_porta(str(addr.numero)) or number

    bairro = None
    if addr.bairro and addr.bairro.strip():
        bairro = re.sub(r"\s+", " ", strip_accents(addr.bairro).upper().strip())

    raw_key = f"{street}|{number or ''}|{bairro or ''}|{city}|{uf}"
    key = hashlib.sha1(raw_key.encode("utf-8")).hexdigest()[:20]
    return NormalizedAddress(street, number, bairro, city, uf, complement, key)
