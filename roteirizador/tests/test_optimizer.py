import os

import pytest

from api.app.models import Address, Depot, GeoResult, Stop, VehicleConfig
from api.app.routing.optimizer import Optimizer, VroomError
from api.app.routing.osrm import OsrmClient
from api.app.routing.vroom import NoGeocodedStops

OSRM = os.environ.get("OSRM_URL", "http://osrm:5000")
VROOM = os.environ.get("VROOM_URL", "http://vroom:3000")

DEPOT = Depot("Matriz", -54.8060, -22.2210)
PONTOS = [(-54.8180, -22.2280), (-54.7990, -22.2240), (-54.8100, -22.2350),
          (-54.7950, -22.2150), (-54.8250, -22.2100), (-54.8020, -22.2400)]


def _stop(i, lon, lat, kind="delivery"):
    a = Address("RUA X", str(i), None, "DOURADOS", "MS", None, "RUA X")
    s = Stop(external_id=f"S{i}", kind=kind, cliente_id=i, cliente_nome=f"C{i}",
             address=a, service_seconds=300)
    s.geo = GeoResult(lon, lat, "high", "street_exact")
    return s


@pytest.fixture(scope="module")
def opt():
    return Optimizer(VROOM, OsrmClient(OSRM))


@pytest.mark.stack
def test_atende_todas_as_paradas_com_frota_folgada(opt):
    stops = [_stop(i, lon, lat) for i, (lon, lat) in enumerate(PONTOS)]
    fleet = [VehicleConfig(id="A", label="Caminhão A", capacity=10, trips=1)]
    sol = opt.solve(stops, fleet, DEPOT)
    atendidas = {s.stop_external_id for r in sol.routes for s in r.steps}
    assert atendidas == {s.external_id for s in stops}
    assert sol.unassigned == []
    assert sol.total_distance_m > 0


@pytest.mark.stack
def test_preenche_geometria_de_cada_rota(opt):
    stops = [_stop(i, lon, lat) for i, (lon, lat) in enumerate(PONTOS[:3])]
    sol = opt.solve(stops, [VehicleConfig(id="A", label="A", capacity=10)], DEPOT)
    assert all(len(r.geometry) > 10 for r in sol.routes if r.steps)


@pytest.mark.stack
def test_capacidade_um_com_seis_viagens_atende_carga_mista(opt):
    """Caso poliguindaste: leva uma caçamba por vez, alterna entrega e coleta."""
    stops = [_stop(i, lon, lat, "delivery" if i % 2 == 0 else "pickup")
             for i, (lon, lat) in enumerate(PONTOS)]
    fleet = [VehicleConfig(id="MB", label="MB 1513", capacity=1, trips=6,
                           shift_start_s=7 * 3600, shift_end_s=19 * 3600)]
    sol = opt.solve(stops, fleet, DEPOT)
    atendidas = {s.stop_external_id for r in sol.routes for s in r.steps}
    assert len(atendidas) >= 4
    for r in sol.routes:
        assert all(step.load_after <= 1 for step in r.steps)


@pytest.mark.stack
def test_frota_insuficiente_devolve_nao_atendidas(opt):
    stops = [_stop(i, lon, lat) for i, (lon, lat) in enumerate(PONTOS)]
    fleet = [VehicleConfig(id="A", label="A", capacity=2, trips=1,
                           shift_start_s=7 * 3600, shift_end_s=8 * 3600)]
    sol = opt.solve(stops, fleet, DEPOT)
    assert len(sol.unassigned) > 0


@pytest.mark.stack
def test_prioridade_alta_e_atendida_antes(opt):
    stops = [_stop(i, lon, lat) for i, (lon, lat) in enumerate(PONTOS)]
    stops[-1].priority = 100
    fleet = [VehicleConfig(id="A", label="A", capacity=3, trips=1,
                           shift_start_s=7 * 3600, shift_end_s=9 * 3600)]
    sol = opt.solve(stops, fleet, DEPOT)
    atendidas = {s.stop_external_id for r in sol.routes for s in r.steps}
    assert stops[-1].external_id in atendidas


@pytest.mark.stack
def test_sem_paradas_geocodificadas_levanta_erro(opt):
    s = _stop(1, 0, 0); s.geo = None
    with pytest.raises(NoGeocodedStops):
        opt.solve([s], [VehicleConfig(id="A", label="A")], DEPOT)


@pytest.mark.stack
def test_frota_vazia_levanta_erro(opt):
    stops = [_stop(0, *PONTOS[0])]
    with pytest.raises(VroomError):
        opt.solve(stops, [], DEPOT)


class _OsrmQuebrado:
    """Simula uma falha do motor de mapas ao medir a rota já decidida pelo
    VROOM — de um tipo que não é OsrmError nem httpx.HTTPError, para provar
    que _fill_geometry não engole nada: uma rota que não pôde ser medida
    nunca pode virar 0 m (isso subestimaria o total e infla a economia
    reportada)."""
    def route(self, coords):
        raise ValueError("motor de mapas fora do ar")


@pytest.mark.stack
def test_falha_ao_medir_rota_nao_e_engolida_como_zero():
    stops = [_stop(i, lon, lat) for i, (lon, lat) in enumerate(PONTOS[:2])]
    fleet = [VehicleConfig(id="A", label="A", capacity=10)]
    quebrado = Optimizer(VROOM, _OsrmQuebrado())
    with pytest.raises(ValueError):
        quebrado.solve(stops, fleet, DEPOT)
