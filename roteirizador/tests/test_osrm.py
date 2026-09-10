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
def test_coordenada_fora_da_malha_levanta_erro(client):
    """Sem limite de snap o OSRM SEMPRE gruda no nó mais próximo do grafo, por
    mais absurda que seja a coordenada. Um lon/lat invertido viraria uma rota
    plausível e errada. O `radiuses` é o que transforma isso em erro alto."""
    with pytest.raises(OsrmError):
        client.route([(-30.0, -30.0), (-31.0, -31.0)])


@pytest.mark.stack
def test_lon_lat_invertido_e_recusado(client):
    """Dourados com lon/lat trocados cai no Atlântico Sul. É o erro mais fácil
    de cometer neste projeto e o mais caro — tem de estourar, não passar."""
    with pytest.raises(OsrmError):
        client.route([(-22.2210, -54.8060), (-22.2280, -54.8180)])


@pytest.mark.stack
def test_ponto_rural_distante_ainda_e_aceito(client):
    """O limite de snap não pode ser tão apertado que recuse entrega em
    chácara. ~12 km ao norte de Dourados, longe de via mapeada."""
    r = client.route([CENTRO, (-54.8060, -22.1100)])
    assert r.distance_m > 0


@pytest.mark.stack
def test_table_tambem_respeita_o_limite_de_snap(client):
    with pytest.raises(OsrmError):
        client.table([CENTRO, (-30.0, -30.0)])


def test_falha_de_transporte_vira_osrm_error():
    """Container do OSRM parado, ainda subindo ou porta errada -- a falha
    operacional mais provável deste stack. Antes desta correção, `_get` só
    convertia falhas de STATUS HTTP (>=400) em OsrmError; uma falha de
    TRANSPORTE (conexão recusada aqui) subia como httpx.ConnectError cru.
    Não depende dos containers reais -- roda sempre, sem marcador."""
    c = OsrmClient("http://127.0.0.1:1", timeout=2.0)
    with pytest.raises(OsrmError):
        c.route([CENTRO, MARCELINO])
