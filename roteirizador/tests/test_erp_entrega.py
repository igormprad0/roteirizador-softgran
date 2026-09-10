# tests/test_erp_entrega.py
from datetime import date

import pytest

from api.app.config import ImportMode, Profile
from api.app.db.firebird import connect
from api.app.erp.entrega_posterior import EntregaPosteriorSource

PICO = date(2024, 12, 9)
ULTIMO = date(2026, 8, 13)
# Dia com o maior número de FLAG_DEV_VENDAS = 1 da tabela (nenhuma das duas
# datas da demo tem uma sequer).
DIA_COM_DEVOLUCAO = date(2020, 7, 8)


# Linha crua do _SQL, com só o que `fetch` lê. Serve para exercitar caminhos
# que as datas reais não exercitam, sem depender do que a base por acaso tem.
_LINHA_BASE = {
    "PCAB_ID": 1, "ID_ENTREGA": 10, "DATA": ULTIMO, "HORA": None,
    "ID_VEICULO": None, "FLAG_DEV_VENDAS": 0, "ID_CLIENTE": 7,
    "CLIENTE_NOME": "CLIENTE X", "LOGRADOURO": "RUA MATO GROSSO, 1973",
    "CLIENTE_ENDERECO": None, "CLIENTE_BAIRRO": "CENTRO",
    "CLIENTE_CIDADE": "DOURADOS", "CLIENTE_UF": "MS", "VENCIMENTO": None,
    "DOCUMENTO": "999", "TIPO_ENTREGA": None, "CF_NOME": "CLIENTE X",
    "CF_ENDERECO": "RUA JATOBA", "CF_NUMERO": "50", "CF_BAIRRO": "JARDIM",
    "CF_CIDADE": "DOURADOS", "CF_UF": "MS", "CF_CEP": "79800000",
    "VEICULO_PLACA": None, "VEICULO_DESCRICAO": None,
}


class _ConnFake:
    def __init__(self, rows):
        self._rows = rows

    def query(self, sql, params=()):
        return self._rows


@pytest.fixture(scope="module")
def src():
    with connect(Profile.ENTREGA_POSTERIOR) as c:
        yield EntregaPosteriorSource(c)


@pytest.mark.erp
def test_perfil(src):
    assert src.profile is Profile.ENTREGA_POSTERIOR


@pytest.mark.erp
def test_replanejar_traz_as_paradas_do_ultimo_dia(src):
    stops = src.fetch(ULTIMO, ImportMode.REPLANEJAR)
    assert 10 <= len(stops) <= 20
    assert all(s.external_id.startswith("EP:") for s in stops)


@pytest.mark.erp
def test_replanejar_traz_o_dia_de_pico(src):
    stops = src.fetch(PICO, ImportMode.REPLANEJAR)
    assert len(stops) > 190


@pytest.mark.erp
def test_producao_nao_traz_nada_em_base_historica(src):
    # SITUACAO = 1 tem zero linhas nesta base (ver §1.2.1 do spec)
    assert src.fetch(ULTIMO, ImportMode.PRODUCAO) == []


@pytest.mark.erp
def test_exclui_tipo_entrega_2_cliente_retira(src):
    with connect(Profile.ENTREGA_POSTERIOR) as c:
        rows = c.query(
            "SELECT FIRST 1 p.DATA FROM ENTREGA_PCAB p"
            " JOIN ENTREGA_CAB e ON e.ID_CONTROLE = p.ID_ENTREGA"
            " WHERE e.TIPO_ENTREGA = 2 AND p.SITUACAO <> 3")
    if not rows:
        pytest.skip("base sem TIPO_ENTREGA=2 fora de cancelados")
    dia = rows[0]["DATA"]
    ids = {s.external_id for s in src.fetch(dia, ImportMode.REPLANEJAR)}
    with connect(Profile.ENTREGA_POSTERIOR) as c:
        excl = c.query(
            "SELECT p.ID_CONTROLE FROM ENTREGA_PCAB p"
            " JOIN ENTREGA_CAB e ON e.ID_CONTROLE = p.ID_ENTREGA"
            " WHERE e.TIPO_ENTREGA = 2 AND p.DATA = ?", (dia,))
    for r in excl:
        assert f"EP:{int(r['ID_CONTROLE'])}" not in ids


@pytest.mark.erp
def test_nunca_traz_cancelados(src):
    stops = src.fetch(PICO, ImportMode.REPLANEJAR)
    with connect(Profile.ENTREGA_POSTERIOR) as c:
        canc = {f"EP:{int(r['ID_CONTROLE'])}" for r in c.query(
            "SELECT ID_CONTROLE FROM ENTREGA_PCAB WHERE DATA = ? AND SITUACAO = 3",
            (PICO,))}
    assert not ({s.external_id for s in stops} & canc)


@pytest.mark.erp
def test_endereco_preenchido_e_cidade_default(src):
    stops = src.fetch(ULTIMO, ImportMode.REPLANEJAR)
    com_rua = [s for s in stops if s.address.logradouro]
    assert len(com_rua) >= len(stops) * 0.8
    assert all(s.address.cidade for s in stops)


def test_devolucao_vira_pickup_com_linha_sintetica():
    """`FLAG_DEV_VENDAS = 1` vira coleta. A versão anterior deste teste
    afirmava `{s.kind} <= {"delivery","pickup"}` contra o dia de pico: uma
    tautologia (não existe terceiro valor de `kind`) sobre um dia que tem
    ZERO devoluções -- o ramo de coleta podia ser código morto que o teste
    passava igual. Aqui a linha é sintética e determinística, então a
    afirmação é sobre o mapeamento e não sobre o que o dia por acaso tem."""
    entrega = dict(_LINHA_BASE, PCAB_ID=1, FLAG_DEV_VENDAS=0)
    devolucao = dict(_LINHA_BASE, PCAB_ID=2, FLAG_DEV_VENDAS=1)
    src = EntregaPosteriorSource(_ConnFake([entrega, devolucao]))
    a, b = src.fetch(ULTIMO, ImportMode.REPLANEJAR)
    assert (a.kind, b.kind) == ("delivery", "pickup")
    # coleta demora mais que entrega -- se o ramo sumir, isto cai junto
    assert b.service_seconds > a.service_seconds


@pytest.mark.erp
def test_devolucao_vira_pickup_num_dia_real(src):
    """O mesmo mapeamento contra a base de verdade. 2020-07-08 é o dia com
    mais `FLAG_DEV_VENDAS = 1` da tabela (4 linhas, de 125 no total); nas
    duas datas da demo não há nenhuma, e era por isso que este caminho
    ficou sem cobertura real o projeto inteiro."""
    stops = src.fetch(DIA_COM_DEVOLUCAO, ImportMode.REPLANEJAR)
    with connect(Profile.ENTREGA_POSTERIOR) as c:
        esperados = {f"EP:{int(r['ID_CONTROLE'])}" for r in c.query(
            "SELECT ID_CONTROLE FROM ENTREGA_PCAB "
            "WHERE DATA = ? AND FLAG_DEV_VENDAS = 1 AND SITUACAO <> 3",
            (DIA_COM_DEVOLUCAO,))}
    assert esperados, "dia escolhido não tem mais devolução -- troque a data"
    coletas = {s.external_id for s in stops if s.kind == "pickup"}
    assert coletas == esperados & {s.external_id for s in stops}
    assert coletas


@pytest.mark.erp
def test_baseline_agrupa_por_veiculo_do_erp(src):
    stops = src.fetch(ULTIMO, ImportMode.REPLANEJAR)
    trips = src.baseline_order(stops, [])
    assert len(trips) >= 4
    total = sum(len(t.stop_external_ids) for t in trips)
    assert total == len(stops)


@pytest.mark.erp
def test_baseline_respeita_a_hora_registrada(src):
    stops = src.fetch(ULTIMO, ImportMode.REPLANEJAR)
    por_id = {s.external_id: s for s in stops}
    for t in src.baseline_order(stops, []):
        seqs = [por_id[i].erp_sequence for i in t.stop_external_ids]
        assert seqs == sorted(seqs)


def test_numero_do_cadastro_nao_gruda_no_endereco_de_entrega():
    """O `numero` do CADASTRO não pode viajar junto de um logradouro que
    veio do endereço de ENTREGA.

    `_LINHA_BASE` é exatamente o caso real: `LOGRADOURO` = "RUA MATO
    GROSSO, 1973" (o número já está no texto) e `CF_NUMERO` = "50" (o
    número da "RUA JATOBA, 50", outro endereço). `normalize.py` dá
    precedência a `addr.numero` sobre o número extraído do texto, então
    colar o do cadastro em cima trocava a porta -- 18 de 18 paradas de
    13/08 iam para o número errado, em silêncio, com a suíte verde.
    """
    entrega, = EntregaPosteriorSource(_ConnFake([_LINHA_BASE])).fetch(
        ULTIMO, ImportMode.REPLANEJAR)
    assert entrega.address.logradouro == "RUA MATO GROSSO, 1973"
    assert entrega.address.numero is None, (
        f"número {entrega.address.numero!r} veio do cadastro e vai "
        f"sobrepor o 1973 que já está no logradouro de entrega")
    # e o do cadastro continua valendo quando é ELE quem dá o logradouro
    so_cadastro = dict(_LINHA_BASE, LOGRADOURO=None, CLIENTE_ENDERECO=None)
    cadastrada, = EntregaPosteriorSource(_ConnFake([so_cadastro])).fetch(
        ULTIMO, ImportMode.REPLANEJAR)
    assert (cadastrada.address.logradouro, cadastrada.address.numero) == (
        "RUA JATOBA", "50")
