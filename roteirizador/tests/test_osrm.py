# tests/test_osrm.py
import os
import pytest
from api.app.routing.osrm import OsrmClient, OsrmError

OSRM = os.environ.get("OSRM_URL", "http://osrm:5000")

CENTRO   = (-54.8060, -22.2210)
MARCELINO = (-54.8180, -22.2280)
JOAO_ROSA = (-54.7990, -22.2240)


@pytest.fixture(scope="module")
def client():
    return OsrmClient(OSRM)


@pytest.mark.stack
def test_table_dimensoes_e_diagonal_zero(client):
    m = client.table([CENTRO, MARCELINO, JOAO_ROSA])
    assert len(m.durations) == 3 and len(m.durations[0]) == 3
    assert len(m.distances) == 3
    assert m.durations[0][0] == 0
    assert m.distances[1][1] == 0


@pytest.mark.stack
def test_table_valores_plausiveis_dentro_de_dourados(client):
    m = client.table([CENTRO, MARCELINO])
    assert 100 < m.distances[0][1] < 20_000        # metros
    assert 10 < m.durations[0][1] < 3_600          # segundos


@pytest.mark.stack
def test_route_devolve_polyline_e_custos(client):
    r = client.route([CENTRO, MARCELINO, JOAO_ROSA])
    assert isinstance(r.polyline, str) and len(r.polyline) > 10
    assert r.distance_m > 0 and r.duration_s > 0


@pytest.mark.stack
def test_route_com_menos_de_dois_pontos_e_vazia(client):
    r = client.route([CENTRO])
    assert r.distance_m == 0 and r.duration_s == 0 and r.polyline == ""


@pytest.mark.stack
def test_nearest_gruda_na_via(client):
    lon, lat = client.nearest((-54.8061, -22.2211))
    assert abs(lon + 54.8) < 0.2 and abs(lat + 22.2) < 0.2


@pytest.mark.stack
def test_coordenada_no_meio_do_oceano_levanta_erro(client):
    with pytest.raises(OsrmError):
        client.route([(-30.0, -30.0), (-31.0, -31.0)])
