import pytest
from api.app.models import Address
from api.app.geo.normalize import (
    NormalizedAddress, _numero_de_porta, expand_abbreviations,
    normalize_address, normalize_city, split_number, strip_accents,
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


# --- Fix round 2/5 do geocoder: abreviação colada ao nome por ponto, sem
# espaço ("AV.MARCELINO", "R.JOAO", "ROD.BR-163"). Achado real: o índice
# tem uma "RUA MARCELINO PIRES" genuína e distinta da avenida; sem expandir
# "AV." aqui, "AV.MARCELINO PIRES" nunca vira "AVENIDA MARCELINO PIRES" e o
# fuzzy score do geocoder escolhe a rua errada. Não altera nenhum dos casos
# acima -- só cobre um formato que a implementação anterior não tratava.
@pytest.mark.parametrize("entrada,esperado", [
    ("AV.MARCELINO PIRES", "AVENIDA MARCELINO PIRES"),
    ("R.JOAO ROSA GOES", "RUA JOAO ROSA GOES"),
    ("ROD.BR-163", "RODOVIA BR-163"),
    ("AL.HECTARES", "ALAMEDA HECTARES"),
    ("MARCELINO PIRES", "MARCELINO PIRES"),  # sem ponto: nao deve mudar
])
def test_expand_abbreviations_abreviacao_colada_sem_espaco(entrada, esperado):
    assert expand_abbreviations(entrada) == esperado


def test_split_number_abreviacao_colada_sem_espaco():
    street, num, _ = split_number("AV.MARCELINO PIRES, 5285")
    assert street == "AVENIDA MARCELINO PIRES"
    assert num == "5285"


# --- Fix round 3/5 do geocoder: o primeiro numero autonomo da string
# encerra o nome da rua -- tudo depois dele (bairro repetido, anotacao
# livre, o mesmo numero de novo) nunca faz parte do nome. Medido contra o
# indice real: em quase todos os enderecos que caiam em "low" na Fix Round
# 2, a rua correta EXISTE no indice (ex.: "RUA ONOFRE PEREIRA DE MATOS"),
# so o texto colado depois do numero derrubava o fuzzy score.
@pytest.mark.parametrize("entrada,rua_esperada", [
    ("RUA ONOFRE PEREIRA DE MATOS,970 CENTRO, 970", "RUA ONOFRE PEREIRA DE MATOS"),
    ("BARAO DO RIO BRANCO 395 JARDIM TROPICAL", "BARAO DO RIO BRANCO"),
    ("CORONEL PONCIANO 1425 NOVA DOURADOS", "CORONEL PONCIANO"),
    ("SANTOS DUMONT MARMITARIA, 1361", "SANTOS DUMONT MARMITARIA"),
    ("Aurora Augusta de Matos, 3600 (fundos Ecov", "AURORA AUGUSTA DE MATOS"),
    ("Rua Antonio E. de Figueiredo, 2280 Esq. Ca", "RUA ANTONIO E. DE FIGUEIREDO"),
    ("RUA PROJETADA A, 285\nC VALDEREZ OLIVEIRA", "RUA PROJETADA A"),
    ("AV: PRESIDENTE VARGAS, LT1 Q 18(4948)", "AVENIDA PRESIDENTE VARGAS"),
    ("ALAMENDA DAS HORTENCIAS 225, 225", "ALAMENDA DAS HORTENCIAS"),
    ("IPANEMA LOTE 06 QUADRA 09, 725", "IPANEMA"),
])
def test_split_number_numero_encerra_o_nome_da_rua(entrada, rua_esperada):
    street, _, _ = split_number(entrada)
    assert street == rua_esperada


@pytest.mark.parametrize("entrada,rua_esperada,numero_esperado", [
    ("RUA 13 DE MAIO 500", "RUA 13 DE MAIO", "500"),
    ("25 DE MARCO 100", "25 DE MARCO", "100"),
    ("AVENIDA 7 DE SETEMBRO 120 CASA 2", "AVENIDA 7 DE SETEMBRO", "120"),
])
def test_split_number_numero_seguido_de_de_e_parte_do_nome(entrada, rua_esperada, numero_esperado):
    """Um numero seguido de ' DE ' (data historica: '13 de Maio', '25 de
    Março', '7 de Setembro' são nomes de rua correntes no Brasil) nunca é o
    numero de casa, mesmo sendo o primeiro numero autonomo da string --
    a varredura continua até achar o número de casa de verdade, mais
    adiante."""
    street, num, _ = split_number(entrada)
    assert (street, num) == (rua_esperada, numero_esperado)


def test_expand_abbreviations_dois_pontos_no_lugar_do_ponto():
    """':' às vezes substitui '.' na abreviação ('AV: PRESIDENTE VARGAS')."""
    assert expand_abbreviations("AV: PRESIDENTE VARGAS") == "AVENIDA PRESIDENTE VARGAS"


def test_split_number_normaliza_quebra_de_linha_e_tab():
    street, num, _ = split_number("RUA PROJETADA A, 285\tC VALDEREZ OLIVEIRA")
    assert street == "RUA PROJETADA A"
    assert num == "285"


@pytest.mark.parametrize("entrada,rua_esperada", [
    ("AVENIDA PRESIDENTE VARGAS, LT1 Q 18(4948)", "AVENIDA PRESIDENTE VARGAS"),
    ("AURORA AUGUSTA DE MATOS, 3600 (FUNDOS ECOVILLE", "AURORA AUGUSTA DE MATOS"),
])
def test_split_number_descarta_parenteses_finais_fechado_ou_truncado(entrada, rua_esperada):
    """Anotação solta entre parênteses no final do campo -- o ERP às vezes
    trunca o campo no meio, sem fechar o parêntese ('(fundos Ecov')."""
    street, _, _ = split_number(entrada)
    assert street == rua_esperada


@pytest.mark.parametrize("numero_erp,esperado", [
    # o que É número de porta
    ("345", "345"), (" 1234 ", "1234"), ("4350-A", "4350"), ("98 B", "98"),
    # o que NÃO é: quadra/lote, sem número, texto solto
    ("Q16/L9", None), ("S/N", None), ("Q03 - LT01A", None),
    ("QD 5 LT 12", None), ("SN", None), ("-", None), ("", None),
])
def test_numero_de_porta_recusa_quadra_lote_e_sem_numero(numero_erp, esperado):
    r"""O campo NUMERO do ERP guarda muito mais que número de porta, e
    `re.sub(r"\D", "", ...)` transformava "Q16/L9" em 169 -- entregue à
    interpolação de número de porta como se fosse a porta 169 da rua. Um
    ponto inventado com cara de precisão. Quando o valor não é número de
    porta, o certo é NÃO haver número: a cascata do geocoder cai para o
    segmento/bairro, que é honesto."""
    assert _numero_de_porta(numero_erp) == esperado


def test_numero_do_cadastro_nao_entra_quando_nao_e_numero_de_porta():
    """Ponta a ponta pelo `normalize_address`: o valor imprestável do
    cadastro não pode nem sobrescrever nem inventar número."""
    addr = Address("RUA MATO GROSSO, 1973", "Q16/L9", "CENTRO",
                   "DOURADOS", "MS", None, "")
    assert normalize_address(addr).number == "1973"     # o do texto sobrevive
    addr_sem_texto = Address("RUA MATO GROSSO", "Q16/L9", "CENTRO",
                             "DOURADOS", "MS", None, "")
    assert normalize_address(addr_sem_texto).number is None
