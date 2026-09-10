# tests/test_erp_locacao.py
from datetime import date

import pytest

from api.app.config import ImportMode, Profile
from api.app.db.firebird import connect
from api.app.erp.base import build_source, cabe
from api.app.erp.locacao import LocacaoSource
from api.app.models import Address, Depot, Stop, VehicleConfig
from api.app.routing.vroom import expand_trips

DIA = date(2026, 8, 4)
DEPOSITO = Depot("Depósito", -54.8060, -22.2210)


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
    """Teto 20: a fila de vencidas (300+) estoura, as 12 devoluções do dia não.
    Se `dropped_pickups` não fosse recalculado a cada fetch, o segundo assert
    veria o resto do primeiro."""
    with connect(Profile.LOCACAO) as c:
        src = LocacaoSource(c, overdue_days=1, max_pickups=20)
        src.fetch(DIA, ImportMode.PRODUCAO)
        assert src.dropped_pickups > 0
        src.fetch(DIA, ImportMode.REPLANEJAR)      # 12 coletas <= teto de 20
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
    """Com uma frota folgada (capacidade que nunca vira gargalo), tudo cabe
    numa única viagem, na ordem de lançamento -- prova que a partição
    respeita `erp_sequence` quando capacidade não é o fator limitante."""
    stops = src.fetch(DIA, ImportMode.REPLANEJAR)
    frota_folgada = [VehicleConfig(id="X", label="X", capacity=1000, trips=1)]
    trips = src.baseline_order(stops, expand_trips(frota_folgada, DEPOSITO))
    assert len(trips) >= 1
    assert sum(len(t.stop_external_ids) for t in trips) == len(stops)
    por_id = {s.external_id: s for s in stops}
    primeira = [por_id[i].erp_sequence for i in trips[0].stop_external_ids]
    assert primeira == sorted(primeira)


@pytest.mark.erp
def test_baseline_respeita_capacidade_da_viagem(src):
    """Achado do coordenador (fix round 1): antes desta correção, o baseline
    empacotava ~12 paradas por viagem ignorando capacidade -- fisicamente
    impossível para uma poliguindaste (capacidade 1, uma caçamba por vez).
    Com a frota real do seed de demo (capacidade 1), toda viagem devolvida
    tem que ser executável: nunca mais de uma entrega "a bordo" ao mesmo
    tempo (duas exigiriam carregar duas caçambas novas simultaneamente)."""
    stops = src.fetch(DIA, ImportMode.REPLANEJAR)
    frota = [
        VehicleConfig(id="MB1513", label="MB1513", capacity=1, trips=8,
                     shift_start_s=7 * 3600, shift_end_s=18 * 3600),
        VehicleConfig(id="TRUCK2", label="Truck reserva", capacity=1, trips=6,
                     shift_start_s=7 * 3600, shift_end_s=17 * 3600),
    ]
    trips = src.baseline_order(stops, expand_trips(frota, DEPOSITO))
    por_id = {s.external_id: s for s in stops}

    # Neste dia (2026-08-04) a ordem de lançamento é [26 entregas][12
    # coletas] (ver `fetch`: entregas vêm todas antes de coletas). Como o
    # despachante varre a fila PARA A FRENTE (pula o que não cabe no
    # caminhão já carregado e leva o próximo que cabe), uma viagem de
    # capacidade 1 pega uma entrega e alcança uma coleta lá adiante --
    # "sai cheio, volta cheio", que é o que a operação real faz. A prova
    # isolada dessa física, sem depender de qual endereço caiu em que
    # posição, está em `tests/test_viabilidade.py::
    # test_cabe_e_a_mesma_fisica_nos_dois_perfis`.
    assert trips, "frota realista deveria produzir pelo menos uma viagem"
    for t in trips:
        viagem = [por_id[i] for i in t.stop_external_ids]
        assert cabe(viagem, 1), (
            f"{t.label} excede capacidade 1: "
            f"{[(s.kind, s.amount) for s in viagem]}")
        n_entregas = sum(1 for s in viagem if s.kind == "delivery")
        assert n_entregas <= 1, f"{t.label} carrega {n_entregas} entregas ao mesmo tempo"

    # a lógica antiga (blocos fixos de ~12) teria devolvido poucas viagens
    # grandes; a física real de capacidade 1 exige muitas viagens curtas --
    # prova de que a correção mudou o comportamento, não só a assinatura.
    assert len(trips) > 5


def _mini_stop(ext_id: str, kind: str, seq: int, amount: int = 1):
    addr = Address(None, None, None, "DOURADOS", "MS", None, "")
    return Stop(external_id=ext_id, kind=kind, cliente_id=1, cliente_nome="C",
               address=addr, amount=amount, erp_sequence=seq)


def test_cabe_permite_entrega_seguida_de_coleta_mas_nao_o_contrario():
    """Prova determinística (dado sintético, sem tocar o ERP) de que a
    capacidade 1 permite "sai cheio, volta cheio": uma entrega pré-carregada
    no depósito, seguida de uma coleta que ocupa o espaço que a entrega
    liberou ao ser descarregada. Mas não permite duas entregas ao mesmo
    tempo (exigiria duas caçambas a bordo), nem a ordem trocada -- coleta
    antes de a entrega ter sido descarregada exigiria 2 itens a bordo ao
    mesmo tempo, mesmo que a entrega só "saia" depois."""
    entrega = _mini_stop("E1", "delivery", 0)
    coleta = _mini_stop("P1", "pickup", 1)
    outra_entrega = _mini_stop("E2", "delivery", 2)

    assert cabe([entrega, coleta], 1)              # sai cheio, volta cheio
    assert not cabe([entrega, outra_entrega], 1)   # 2 entregas ao mesmo tempo
    assert not cabe([coleta, entrega], 1)          # ordem errada, capacidade 1
    assert cabe([coleta, entrega], 2)              # capacidade 2 já tolera a troca


def test_baseline_order_encadeia_entrega_e_coleta_numa_so_viagem_sintetica():
    """Mesma prova, mas passando pelo caminho real (`baseline_order`, não só
    `cabe` isolado): com uma entrega seguida de uma coleta na ordem de
    lançamento e uma frota de capacidade 1, as duas paradas caem na MESMA
    viagem -- o comportamento que a correção do coordenador pediu."""
    stops = [_mini_stop("E1", "delivery", 0), _mini_stop("P1", "pickup", 1)]
    frota = [VehicleConfig(id="X", label="X", capacity=1, trips=1)]
    # `baseline_order` não toca `self._conn` (só `fetch` toca) -- não precisa
    # de conexão real com o Firebird para este teste sintético e determinístico.
    trips = LocacaoSource(None).baseline_order(stops, expand_trips(frota, DEPOSITO))
    assert len(trips) == 1
    assert trips[0].stop_external_ids == ["E1", "P1"]


@pytest.mark.erp
def test_build_source_resolve_os_dois_perfis():
    with connect(Profile.LOCACAO) as c:
        assert build_source(Profile.LOCACAO, c).profile is Profile.LOCACAO
    with connect(Profile.ENTREGA_POSTERIOR) as c:
        assert build_source(Profile.ENTREGA_POSTERIOR, c).profile \
            is Profile.ENTREGA_POSTERIOR
