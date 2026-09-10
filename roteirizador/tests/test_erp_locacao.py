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
def test_a_locacao_nao_tem_o_problema_da_base_historica(src):
    """Diferente da entrega posterior, aqui PRODUCAO devolve dados: as
    entregas vêm por DATA_LOCACAO, que não depende de nada estar pendente."""
    assert len(src.fetch(DIA, ImportMode.PRODUCAO)) > 0


@pytest.mark.erp
def test_replanejar_traz_as_devolucoes_reais_do_dia(src):
    """DATA_DEVOLUCAO é preenchida quando a coleta acontece, então num dia
    histórico ela É a carga de coleta real. Em 2026-08-04: 26 entregas, 12
    coletas — o caminhão sai cheio e volta cheio de verdade."""
    stops = src.fetch(DIA, ImportMode.REPLANEJAR)
    entregas = [s for s in stops if s.kind == "delivery"]
    coletas = [s for s in stops if s.kind == "pickup"]
    assert len(entregas) == 26
    assert len(coletas) == 12
    with connect(Profile.LOCACAO) as c:
        esperadas = {f"LOC:{int(r['ID_SEQUENCIA'])}" for r in c.query(
            "SELECT ID_SEQUENCIA FROM LOCACAO_PRODUTO WHERE DATA_DEVOLUCAO = ?",
            (DIA,))}
    assert {s.external_id for s in coletas} == esperadas


@pytest.mark.erp
def test_replanejar_nao_infla_prioridade_com_tempo_de_locacao(src):
    """Em dia histórico, dias na rua não é atraso — não deve virar prioridade."""
    coletas = [s for s in src.fetch(DIA, ImportMode.REPLANEJAR) if s.kind == "pickup"]
    assert all(s.days_overdue == 0 for s in coletas)
    assert all(0 <= s.priority <= 100 for s in coletas)


@pytest.mark.erp
def test_producao_traz_a_fila_de_vencidas_nao_as_devolvidas(src):
    """Em produção a coleta é o backlog: segue em locação e passou do corte.
    Em 2026-08-04 com 30 dias de corte são 4."""
    with connect(Profile.LOCACAO) as c:
        coletas = [s for s in LocacaoSource(c, overdue_days=30)
                   .fetch(DIA, ImportMode.PRODUCAO) if s.kind == "pickup"]
    assert len(coletas) == 4
    assert all(s.days_overdue > 30 for s in coletas)
    assert all(s.priority > 0 for s in coletas)


@pytest.mark.erp
def test_producao_nunca_coleta_algo_ja_devolvido(src):
    with connect(Profile.LOCACAO) as c:
        ids = {s.external_id for s in LocacaoSource(c, overdue_days=30)
               .fetch(DIA, ImportMode.PRODUCAO) if s.kind == "pickup"}
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
def test_teto_de_coletas_e_reportado_nao_engolido():
    """Com corte de 1 dia a fila de vencidas passa de 300 — é aí que o teto
    entra. Com 30 dias são só 4 e ele nunca dispara, então o teste tem de usar
    o corte curto, senão não testa nada."""
    with connect(Profile.LOCACAO) as c:
        apertado = LocacaoSource(c, overdue_days=1, max_pickups=5)
        stops = apertado.fetch(DIA, ImportMode.PRODUCAO)
        folgado = LocacaoSource(c, overdue_days=1, max_pickups=10_000)
        todas = folgado.fetch(DIA, ImportMode.PRODUCAO)
    n_apertado = len([s for s in stops if s.kind == "pickup"])
    n_todas = len([s for s in todas if s.kind == "pickup"])
    assert n_todas > 100, "corte de 1 dia deveria render uma fila grande"
    assert n_apertado == 5
    assert apertado.dropped_pickups == n_todas - 5
    assert folgado.dropped_pickups == 0


@pytest.mark.erp
def test_dropped_pickups_reseta_entre_fetches():
    with connect(Profile.LOCACAO) as c:
        src = LocacaoSource(c, overdue_days=1, max_pickups=5)
        src.fetch(DIA, ImportMode.PRODUCAO)
        assert src.dropped_pickups > 0
        src.fetch(DIA, ImportMode.REPLANEJAR)      # 12 coletas, cabe no teto
        assert src.dropped_pickups == 0


@pytest.mark.erp
def test_overdue_days_configuravel():
    with connect(Profile.LOCACAO) as c:
        curto = LocacaoSource(c, overdue_days=1, max_pickups=10_000) \
            .fetch(DIA, ImportMode.PRODUCAO)
        longo = LocacaoSource(c, overdue_days=365, max_pickups=10_000) \
            .fetch(DIA, ImportMode.PRODUCAO)
    n_curto = len([s for s in curto if s.kind == "pickup"])
    n_longo = len([s for s in longo if s.kind == "pickup"])
    assert n_curto > n_longo


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
