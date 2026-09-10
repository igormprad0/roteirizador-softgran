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
# "N/C" | "S/N" | "SN" no fim (com ou sem virgula antes) -> sem numero
_SEM_NUMERO = re.compile(r",?\s*(?:N/C|S/N|SN)\s*$")
# numero no fim da string, com separador opcional por virgula/"N"/"Nº"/espaco,
# tolerando sufixo "-A" e um trecho extra apos virgula (", ESPLANADA").
# Nota: strip_accents(NFKD) decompoe "º" (ordinal masculino) para uma letra
# "O" solta (nao e um combining mark, entao nao e removido) -- por isso "N"
# tambem aceita ser seguido de "O" aqui.
_NUMERO = re.compile(
    r"(?:,\s*|\s+N[Oº°.]?\s*|\s+)(\d{1,6})(?:\s*-\s*[A-Z0-9]+)?\s*(?:,[^,]*)?$")


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


def expand_abbreviations(s: str) -> str:
    parts = re.sub(r"\s+", " ", s).strip().split(" ")
    if parts and parts[0].upper() in _ABBREV:
        parts[0] = _ABBREV[parts[0].upper()]
    return " ".join(parts)


def split_number(s: str) -> tuple[str, str | None, str | None]:
    """Devolve (rua, numero, complemento). Aceita texto cru; normaliza internamente."""
    s = expand_abbreviations(strip_accents(s or "").upper())

    complement = None
    quadras = _QUADRA.findall(s)
    if quadras:
        complement = " ".join(q.strip() for q in quadras)
        s = _QUADRA.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip().strip(",").strip()

    s = _SEM_NUMERO.sub("", s).strip().strip(",").strip()

    number = None
    m = _NUMERO.search(s)
    if m:
        number = m.group(1)
        s = s[: m.start()].strip().strip(",").strip()
    return s, number, complement


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
        number = re.sub(r"\D", "", str(addr.numero)) or number

    bairro = None
    if addr.bairro and addr.bairro.strip():
        bairro = re.sub(r"\s+", " ", strip_accents(addr.bairro).upper().strip())

    raw_key = f"{street}|{number or ''}|{bairro or ''}|{city}|{uf}"
    key = hashlib.sha1(raw_key.encode("utf-8")).hexdigest()[:20]
    return NormalizedAddress(street, number, bairro, city, uf, complement, key)
