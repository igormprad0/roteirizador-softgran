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
