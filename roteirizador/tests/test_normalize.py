import pytest
from api.app.models import Address
from api.app.geo.normalize import (
    NormalizedAddress, expand_abbreviations, normalize_address,
    normalize_city, split_number, strip_accents,
)


def _addr(raw, **kw):
    base = dict(logradouro=raw, numero=None, bairro=None,
                cidade=None, uf=None, cep=None, raw=raw)
    base.update(kw)
    return Address(**base)


def test_strip_accents():
    assert strip_accents("RUA JOÃO ROSA GOES") == "RUA JOAO ROSA GOES"
    assert strip_accents("ITAPORÃ") == "ITAPORA"


@pytest.mark.parametrize("entrada,esperado", [
    ("DOURADOS", ("DOURADOS", "MS")),
    ("DDOS", ("DOURADOS", "MS")),
    ("DORUADOS", ("DOURADOS", "MS")),
    ("DOURADOS - MS", ("DOURADOS", "MS")),
    ("DOURADOS-MS", ("DOURADOS", "MS")),
    ("  dourados  ", ("DOURADOS", "MS")),
    ("CAMPO GRANDE", ("CAMPO GRANDE", "MS")),
    ("ITAPORA", ("ITAPORA", "MS")),
    (None, ("DOURADOS", "MS")),
    ("", ("DOURADOS", "MS")),
])
def test_normalize_city(entrada, esperado):
    assert normalize_city(entrada) == esperado


@pytest.mark.parametrize("entrada,esperado", [
    ("R. JOAO ROSA GOES", "RUA JOAO ROSA GOES"),
    ("AV  WEIMAR G TORRES", "AVENIDA WEIMAR G TORRES"),
    ("ROD DDOS A ITAPORA", "RODOVIA DDOS A ITAPORA"),
    ("AL HECTARES", "ALAMEDA HECTARES"),
    ("TRAV SAO JOSE", "TRAVESSA SAO JOSE"),
    ("PC ANTONIO JOAO", "PRACA ANTONIO JOAO"),
    ("MARCELINO PIRES", "MARCELINO PIRES"),
])
def test_expand_abbreviations(entrada, esperado):
    assert expand_abbreviations(entrada) == esperado


@pytest.mark.parametrize("entrada,rua,numero", [
    ("RUA MATO GROSSO, 1973", "RUA MATO GROSSO", "1973"),
    ("MARCELINO PIRES 4350-A", "MARCELINO PIRES", "4350"),
    ("ALAMEDA HECTARES Nº 665", "ALAMEDA HECTARES", "665"),
    ("ALAMEDA HECTARES N 665", "ALAMEDA HECTARES", "665"),
    ("RUA CURICACA, 1470, ESPLANADA", "RUA CURICACA", "1470"),
    ("SUICA, N/C", "SUICA", None),
    ("HECTARES", "HECTARES", None),
])
def test_split_number(entrada, rua, numero):
    street, num, _ = split_number(entrada)
    assert (street, num) == (rua, numero)


def test_split_number_guarda_quadra_lote_como_complemento():
    street, num, comp = split_number("RUA TARANTO Q 13 LT 06, 105")
    assert street == "RUA TARANTO"
    assert num == "105"
    assert comp is not None and "Q 13" in comp


def test_normalize_endereco_completo():
    n = normalize_address(_addr("RUA ONOFRE PEREIRA DE MATOS,970 CENTRO, 970",
                                bairro="CENTRO", cidade="DDOS"))
    assert n.street.startswith("RUA ONOFRE PEREIRA DE MATOS")
    assert n.number == "970"
    assert n.city == "DOURADOS"
    assert n.uf == "MS"
    assert n.bairro == "CENTRO"


def test_normalize_usa_cidade_padrao_quando_erp_nao_informa():
    n = normalize_address(_addr("RUA PONTA PORA, 160"))
    assert (n.city, n.uf) == ("DOURADOS", "MS")


@pytest.mark.parametrize("raw", [
    "HECTARES", "LOCALIZACAO PORTAL", "JM EVENTO - ROD DDOS A ITAPORA", "", "   ",
])
def test_normalize_endereco_degenerado_nao_explode(raw):
    n = normalize_address(_addr(raw))
    assert isinstance(n, NormalizedAddress)
    assert n.key


def test_key_e_estavel_e_ignora_variacao_de_escrita():
    a = normalize_address(_addr("R. Mato Grosso, 1973", cidade="DOURADOS"))
    b = normalize_address(_addr("RUA  MATO GROSSO 1973", cidade="DDOS"))
    assert a.key == b.key


def test_key_difere_por_numero():
    a = normalize_address(_addr("RUA MATO GROSSO, 1973"))
    b = normalize_address(_addr("RUA MATO GROSSO, 1975"))
    assert a.key != b.key


def test_numero_do_campo_estruturado_tem_precedencia():
    n = normalize_address(_addr("RUA MATO GROSSO", numero="1973"))
    assert n.number == "1973"


def test_uf_do_campo_estruturado_tem_precedencia():
    n = normalize_address(_addr("RUA X, 1", cidade="SAO PAULO", uf="SP"))
    assert n.uf == "SP"


# --- Casos adicionados na revisao: numero de casa correto quando ha
# CASA/APTO/BLOCO/FUNDOS/KM no meio do endereco. Nao alteram nenhum dos 34
# casos acima; apenas cobrem formatos reais que a implementacao original
# (a do brief) processava errado -- ver relatorio de correcao para o
# raciocinio por tras de cada valor esperado.
@pytest.mark.parametrize("entrada,rua,numero,complemento", [
    ("RUA A 100 CASA 2", "RUA A", "100", "CASA 2"),
    ("AV MARCELINO PIRES, 123, APTO 4, BLOCO B",
     "AVENIDA MARCELINO PIRES", "123", "APTO 4, BLOCO B"),
    ("RUA X, 123, CASA B", "RUA X", "123", "CASA B"),
    ("RUA X 45 FUNDOS", "RUA X", "45", "FUNDOS"),
    ("RUA A, KM 5", "RUA A, KM 5", None, None),
    ("ROD BR-163, KM 12", "RODOVIA BR-163, KM 12", None, None),
    ("RUA MARILIA S/N", "RUA MARILIA", None, None),
    # --- rodada 2 da revisao: CASA/APTO/BLOCO/FUNDOS so sao complemento
    # quando aparecem depois do primeiro digito da string. Antes disso sao
    # nome de rua de verdade -- "Rua Casa Forte" existe em Recife.
    ("RUA CASA FORTE 200", "RUA CASA FORTE", "200", None),
    ("RUA BLOCO B", "RUA BLOCO B", None, None),
    ("RUA A 100 CASA", "RUA A", "100", "CASA"),
    ("ROD BR-163 KM 12 CASA 3", "RODOVIA BR-163 KM 12", None, "CASA 3"),
    ("RUA TARANTO Q 13 LT 06, 105 CASA 2", "RUA TARANTO", "105", "Q 13, LT 06, CASA 2"),
])
def test_split_number_casos_adicionais_complemento_e_km(entrada, rua, numero, complemento):
    street, num, comp = split_number(entrada)
    assert (street, num, comp) == (rua, numero, complemento)
