# tests/test_local_store.py
import pytest
from api.app.config import Profile
from api.app.db.local import LocalStore
from api.app.models import Depot, GeoResult, VehicleConfig


@pytest.fixture
def store(tmp_path):
    s = LocalStore(tmp_path / "t.db")
    s.init_schema()
    return s


def test_geocode_roundtrip(store):
    g = GeoResult(-54.80, -22.22, "high", "street_exact", "RUA MATO GROSSO", 100.0)
    store.put_geocode("k1", g)
    got = store.get_geocode("k1")
    assert (got.lon, got.lat) == (-54.80, -22.22)
    assert got.source == "cache"
    assert got.confidence == "high"


def test_geocode_ausente_retorna_none(store):
    assert store.get_geocode("nao-existe") is None


def test_pin_manual_nao_e_sobrescrito_por_put(store):
    store.put_geocode("k1", GeoResult(-54.80, -22.22, "low", "cidade"))
    store.pin_geocode("k1", -54.81, -22.23)
    store.put_geocode("k1", GeoResult(-54.90, -22.90, "high", "street_exact"))
    got = store.get_geocode("k1")
    assert (got.lon, got.lat) == (-54.81, -22.23)
    assert got.source == "manual"
    assert got.confidence == "high"


def test_fleet_roundtrip(store):
    fleet = [VehicleConfig(id="MB1513", label="MB 1513", placa="KTD3645",
                           capacity=1, trips=6, erp_id_veiculo=2)]
    store.put_fleet(Profile.LOCACAO, fleet)
    got = store.get_fleet(Profile.LOCACAO)
    assert len(got) == 1
    assert got[0].trips == 6
    assert got[0].capacity == 1
    assert store.get_fleet(Profile.ENTREGA_POSTERIOR) == []


def test_put_fleet_substitui_o_conjunto_anterior(store):
    store.put_fleet(Profile.LOCACAO, [VehicleConfig(id="A", label="A")])
    store.put_fleet(Profile.LOCACAO, [VehicleConfig(id="B", label="B")])
    assert [v.id for v in store.get_fleet(Profile.LOCACAO)] == ["B"]


def test_depot_roundtrip(store):
    store.put_depot(Profile.LOCACAO, Depot("Matriz", -54.8060, -22.2210, "Rua Ponta Porã, 1343"))
    d = store.get_depot(Profile.LOCACAO)
    assert d.label == "Matriz"
    assert d.coord == (-54.8060, -22.2210)
