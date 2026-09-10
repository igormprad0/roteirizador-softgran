import pytest

from api.app.models import (Address, BaselineTrip, Depot, GeoResult, RouteStep,
                            Solution, Stop, VehicleRoute)
from api.app.routing.baseline import compare, measure_baseline

DEPOT = Depot("Matriz", -54.8060, -22.2210)


class FakeOsrm:
    """Cobra 1000 m e 120 s por perna, para tornar o cálculo verificável à mão."""
    def __init__(self):
        self.chamadas = []

    def route(self, coords):
        from api.app.routing.osrm import RouteGeometry
        self.chamadas.append(list(coords))
        pernas = max(len(coords) - 1, 0)
        return RouteGeometry("xx", 1000 * pernas, 120 * pernas)


def _stop(sid, service=600):
    a = Address("RUA X", "1", None, "DOURADOS", "MS", None, "RUA X, 1")
    s = Stop(external_id=sid, kind="delivery", cliente_id=1, cliente_nome="C",
             address=a, service_seconds=service)
    s.geo = GeoResult(-54.80, -22.22, "high", "street_exact")
    return s


def test_measure_fecha_o_circuito_no_deposito():
    osrm = FakeOsrm()
    stops = [_stop("a"), _stop("b")]
    trips = [BaselineTrip("Veículo 1", ["a", "b"])]
    measure_baseline(trips, stops, osrm, DEPOT)
    assert osrm.chamadas[0][0] == DEPOT.coord
    assert osrm.chamadas[0][-1] == DEPOT.coord
    assert len(osrm.chamadas[0]) == 4          # depósito + 2 paradas + depósito


def test_measure_soma_distancia_e_inclui_tempo_de_servico():
    stops = [_stop("a", 600), _stop("b", 600)]
    trips = [BaselineTrip("Veículo 1", ["a", "b"])]
    r = measure_baseline(trips, stops, FakeOsrm(), DEPOT)
    assert r.total_distance_m == 3000           # 3 pernas
    assert r.total_duration_s == 3 * 120 + 1200 # deslocamento + serviço
    assert r.vehicles_used == 1


def test_measure_ignora_parada_sem_geocodificacao():
    a, b = _stop("a"), _stop("b")
    b.geo = None
    r = measure_baseline([BaselineTrip("V1", ["a", "b"])], [a, b], FakeOsrm(), DEPOT)
    assert r.total_distance_m == 2000           # só depósito -> a -> depósito


def test_measure_sem_paradas_nao_conta_veiculo():
    r = measure_baseline([BaselineTrip("V1", [])], [], FakeOsrm(), DEPOT)
    assert r.vehicles_used == 0 and r.total_distance_m == 0


def test_measure_marca_aproximado_quando_pedido():
    r = measure_baseline([], [], FakeOsrm(), DEPOT, approximate=True,
                         note="ordem de lançamento")
    assert r.approximate is True
    assert "lançamento" in r.note


def test_compare_calcula_economia():
    sol = Solution(routes=[VehicleRoute("A#1", "A", "A", 1, [], 8000, 3600)],
                   total_distance_m=8000, total_duration_s=3600)
    base = measure_baseline([BaselineTrip("V1", ["a", "b"])],
                            [_stop("a"), _stop("b")], FakeOsrm(), DEPOT)
    c = compare(sol, base, cost_per_km=4.00, workdays=20)
    assert c.baseline_km == pytest.approx(3.0)
    assert c.optimized_km == pytest.approx(8.0)
    assert c.km_saved == pytest.approx(-5.0)          # otimizado pior: reporta negativo
    assert c.monthly_brl_saved == pytest.approx(-5.0 * 4.00 * 20)


def test_compare_percentual_com_baseline_zero_nao_divide_por_zero():
    sol = Solution(total_distance_m=0, total_duration_s=0)
    base = measure_baseline([], [], FakeOsrm(), DEPOT)
    c = compare(sol, base)
    assert c.percent_km_saved == 0.0


def test_compare_propaga_a_ressalva_do_baseline():
    sol = Solution(total_distance_m=1000, total_duration_s=60)
    base = measure_baseline([], [], FakeOsrm(), DEPOT, approximate=True, note="aprox")
    c = compare(sol, base)
    assert c.approximate is True and c.note == "aprox"


# --------------------------------------------------------------------------
# A ordem do baseline: registrada x vizinho mais próximo
#
# A ordem que o ERP registra não carrega informação espacial nenhuma -- 10
# embaralhamentos aleatórios das paradas de cada dia medem o mesmo que ela
# (pico 401,5 km contra 407,4 de média aleatória; 13/08 278,6 contra 283,3;
# locação 119,0 contra 121,8). Um despachante de verdade dirige para a
# parada mais próxima; economia medida só contra um sorteio não é economia.
class GeoOsrm:
    """Distância euclidiana no plano lon/lat, para a ordem importar."""
    def __init__(self):
        self.chamadas = []

    def route(self, coords):
        from api.app.routing.osrm import RouteGeometry
        self.chamadas.append(list(coords))
        d = sum(((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
                for a, b in zip(coords, coords[1:]))
        return RouteGeometry("xx", int(round(d * 100000)), int(round(d * 1000)))


def _stop_em(sid, lon, lat, kind="delivery"):
    a = Address("RUA X", None, None, "DOURADOS", "MS", None, "RUA X")
    s = Stop(external_id=sid, kind=kind, cliente_id=1, cliente_nome="C",
             address=a, service_seconds=0)
    s.geo = GeoResult(lon, lat, "high", "street_exact")
    return s


def _tres_em_linha():
    """Depósito na origem; a ordem registrada vai ao mais distante primeiro."""
    return [_stop_em("a", -54.7, -22.0), _stop_em("b", -54.9, -22.0),
            _stop_em("c", -54.8, -22.0)]


DEPOT0 = Depot("Matriz", -55.0, -22.0)


def test_baseline_fica_com_a_ordem_mais_curta_das_duas():
    stops = _tres_em_linha()
    trip = BaselineTrip("V1", ["a", "b", "c"])
    osrm = GeoOsrm()
    r = measure_baseline([trip], stops, osrm, DEPOT0, capacidade=3)

    # registrada: 0,3 + 0,2 + 0,1 + 0,2 = 0,8 -- vizinho: 0,1+0,1+0,1+0,3 = 0,6
    assert r.total_distance_m == 60000
    assert trip.method == "vizinho_mais_proximo"
    assert r.method == "vizinho_mais_proximo"
    # a viagem PASSA A SER a ordem medida: quem lê stop_external_ids (payload,
    # teste de viabilidade do e2e) vê a mesma sequência que foi cronometrada
    assert trip.stop_external_ids == ["b", "c", "a"]
    # uma chamada `route` extra por viagem, e NENHUMA matriz
    assert len(osrm.chamadas) == 2
    assert not hasattr(osrm, "table_chamada")


def test_baseline_mantem_a_ordem_registrada_quando_ela_e_melhor():
    stops = _tres_em_linha()
    trip = BaselineTrip("V1", ["b", "c", "a"])          # já é a do vizinho
    r = measure_baseline([trip], stops, GeoOsrm(), DEPOT0, capacidade=3)
    assert trip.method == "registrada"
    assert r.method == "registrada"
    assert trip.stop_external_ids == ["b", "c", "a"]


def test_sem_capacidade_o_baseline_nao_reordena_nada():
    """O lado OTIMIZADO também é medido por esta função (a sequência que o
    VROOM decidiu). Reordenar lá seria medir uma rota que o otimizador não
    produziu -- por isso a alternativa só existe quando a capacidade é
    informada, que é o único lugar onde ela faz sentido: o baseline."""
    stops = _tres_em_linha()
    trip = BaselineTrip("V1", ["a", "b", "c"])
    osrm = GeoOsrm()
    r = measure_baseline([trip], stops, osrm, DEPOT0)
    assert r.total_distance_m == 80000
    assert trip.method == "registrada" and len(osrm.chamadas) == 1


def test_vizinho_mais_proximo_obedece_a_fisica_de_carga():
    """Com capacidade 1, a coleta mais próxima do depósito não pode vir
    antes da entrega ("sai cheio, volta cheio" só funciona nessa ordem). O
    guloso não fecha a viagem, e aí vale a ordem registrada -- o baseline
    nunca supõe um caminhão que não existe."""
    entrega = _stop_em("e", -54.7, -22.0)
    coleta = _stop_em("c", -54.9, -22.0, kind="pickup")
    trip = BaselineTrip("V1", ["e", "c"])
    osrm = GeoOsrm()
    measure_baseline([trip], [entrega, coleta], osrm, DEPOT0, capacidade=1)
    assert trip.method == "registrada"
    assert trip.stop_external_ids == ["e", "c"]
    assert len(osrm.chamadas) == 1


def test_compare_publica_contra_qual_ordenacao_o_numero_foi_medido():
    stops = _tres_em_linha()
    base = measure_baseline([BaselineTrip("V1", ["a", "b", "c"])], stops,
                            GeoOsrm(), DEPOT0, capacidade=3)
    sol = Solution(total_distance_m=30000, total_duration_s=600)
    assert compare(sol, base).baseline_method == "vizinho_mais_proximo"
