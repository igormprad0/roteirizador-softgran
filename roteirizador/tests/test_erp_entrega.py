# tests/test_erp_entrega.py
from datetime import date

import pytest

from api.app.config import ImportMode, Profile
from api.app.db.firebird import connect
from api.app.erp.entrega_posterior import EntregaPosteriorSource

PICO = date(2024, 12, 9)
ULTIMO = date(2026, 8, 13)


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


@pytest.mark.erp
def test_devolucao_vira_pickup(src):
    stops = src.fetch(PICO, ImportMode.REPLANEJAR)
    assert {s.kind for s in stops} <= {"delivery", "pickup"}


@pytest.mark.erp
def test_baseline_agrupa_por_veiculo_do_erp(src):
    stops = src.fetch(ULTIMO, ImportMode.REPLANEJAR)
    trips = src.baseline_order(stops)
    assert len(trips) >= 4
    total = sum(len(t.stop_external_ids) for t in trips)
    assert total == len(stops)


@pytest.mark.erp
def test_baseline_respeita_a_hora_registrada(src):
    stops = src.fetch(ULTIMO, ImportMode.REPLANEJAR)
    por_id = {s.external_id: s for s in stops}
    for t in src.baseline_order(stops):
        seqs = [por_id[i].erp_sequence for i in t.stop_external_ids]
        assert seqs == sorted(seqs)
