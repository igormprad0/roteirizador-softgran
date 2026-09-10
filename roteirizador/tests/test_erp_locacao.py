# tests/test_erp_locacao.py
from datetime import date

import pytest

from api.app.config import ImportMode, Profile
from api.app.db.firebird import connect
from api.app.erp.base import build_source
from api.app.erp.locacao import LocacaoSource

DIA = date(2026, 8, 4)


@pytest.fixture(scope="module")
def src():
    with connect(Profile.LOCACAO) as c:
        yield LocacaoSource(c)


@pytest.mark.erp
def test_perfil(src):
    assert src.profile is Profile.LOCACAO


@pytest.mark.erp
def test_traz_entregas_do_dia(src):
    stops = src.fetch(DIA, ImportMode.REPLANEJAR)
    entregas = [s for s in stops if s.kind == "delivery"]
    assert len(entregas) >= 10
    assert all(s.external_id.startswith("LOC:") for s in entregas)


@pytest.mark.erp
def test_traz_coletas_de_locacoes_vencidas(src):
    stops = src.fetch(DIA, ImportMode.REPLANEJAR)
    coletas = [s for s in stops if s.kind == "pickup"]
    assert len(coletas) > 0
    assert all(s.days_overdue > 0 for s in coletas)


@pytest.mark.erp
def test_prioridade_cresce_com_o_atraso(src):
    coletas = [s for s in src.fetch(DIA, ImportMode.REPLANEJAR) if s.kind == "pickup"]
    mais_atrasada = max(coletas, key=lambda s: s.days_overdue)
    menos_atrasada = min(coletas, key=lambda s: s.days_overdue)
    assert mais_atrasada.priority >= menos_atrasada.priority
    assert all(0 <= s.priority <= 100 for s in coletas)


@pytest.mark.erp
def test_nao_traz_locacao_ja_devolvida_como_coleta(src):
    stops = src.fetch(DIA, ImportMode.REPLANEJAR)
    ids = {s.external_id for s in stops if s.kind == "pickup"}
    with connect(Profile.LOCACAO) as c:
        devolvidas = {f"LOC:{int(r['ID_SEQUENCIA'])}" for r in c.query(
            "SELECT FIRST 500 ID_SEQUENCIA FROM LOCACAO_PRODUTO WHERE SITUACAO = 2")}
    assert not (ids & devolvidas)


@pytest.mark.erp
def test_usa_endereco_entrega_quando_existe(src):
    stops = src.fetch(DIA, ImportMode.REPLANEJAR)
    assert any(s.address.raw and s.address.raw.strip() for s in stops)


@pytest.mark.erp
def test_cai_no_cadastro_do_clifor_quando_endereco_entrega_esta_vazio(src):
    with connect(Profile.LOCACAO) as c:
        rows = c.query(
            "SELECT FIRST 1 DATA_LOCACAO FROM LOCACAO_PRODUTO"
            " WHERE (ENDERECO_ENTREGA IS NULL OR CHAR_LENGTH(TRIM(ENDERECO_ENTREGA)) <= 3)"
            " AND DATA_LOCACAO IS NOT NULL ORDER BY DATA_LOCACAO DESC")
    if not rows:
        pytest.skip("base sem endereço de entrega vazio")
    stops = src.fetch(rows[0]["DATA_LOCACAO"], ImportMode.REPLANEJAR)
    assert any(s.address.logradouro for s in stops)


@pytest.mark.erp
def test_teto_de_coletas_e_reportado_nao_engolido(src):
    with connect(Profile.LOCACAO) as c:
        apertado = LocacaoSource(c, overdue_days=30, max_pickups=5)
        stops = apertado.fetch(DIA, ImportMode.REPLANEJAR)
        folgado = LocacaoSource(c, overdue_days=30, max_pickups=10_000)
        todas = folgado.fetch(DIA, ImportMode.REPLANEJAR)
    n_apertado = len([s for s in stops if s.kind == "pickup"])
    n_todas = len([s for s in todas if s.kind == "pickup"])
    assert n_apertado == 5
    assert apertado.dropped_pickups == n_todas - 5
    assert folgado.dropped_pickups == 0


@pytest.mark.erp
def test_overdue_days_configuravel(src):
    with connect(Profile.LOCACAO) as c:
        curto = LocacaoSource(c, overdue_days=1).fetch(DIA, ImportMode.REPLANEJAR)
        longo = LocacaoSource(c, overdue_days=365).fetch(DIA, ImportMode.REPLANEJAR)
    n_curto = len([s for s in curto if s.kind == "pickup"])
    n_longo = len([s for s in longo if s.kind == "pickup"])
    assert n_curto >= n_longo


@pytest.mark.erp
def test_baseline_particiona_pela_ordem_de_lancamento(src):
    stops = src.fetch(DIA, ImportMode.REPLANEJAR)
    trips = src.baseline_order(stops)
    assert len(trips) >= 1
    assert sum(len(t.stop_external_ids) for t in trips) == len(stops)
    por_id = {s.external_id: s for s in stops}
    primeira = [por_id[i].erp_sequence for i in trips[0].stop_external_ids]
    assert primeira == sorted(primeira)


@pytest.mark.erp
def test_build_source_resolve_os_dois_perfis():
    with connect(Profile.LOCACAO) as c:
        assert build_source(Profile.LOCACAO, c).profile is Profile.LOCACAO
    with connect(Profile.ENTREGA_POSTERIOR) as c:
        assert build_source(Profile.ENTREGA_POSTERIOR, c).profile \
            is Profile.ENTREGA_POSTERIOR
