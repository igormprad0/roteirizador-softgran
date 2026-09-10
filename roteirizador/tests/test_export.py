# tests/test_export.py
import csv
import io
from urllib.parse import parse_qs, urlparse

import pytest

from api.app.export.maps_link import google_maps_link, waze_link
from api.app.export.romaneio import build_csv, build_romaneio_pdf
from api.app.models import Address, Depot, RouteStep, Stop, VehicleRoute

DEPOT = Depot("Matriz", -54.8060, -22.2210)


def _step(seq, ext, lon, lat, kind="delivery", arrival=28800):
    return RouteStep(seq=seq, stop_external_id=ext, kind=kind, lon=lon, lat=lat,
                     arrival_s=arrival, load_after=0)


def _stop(ext, nome="CLIENTE X", rua="RUA MATO GROSSO, 1973", kind="delivery"):
    a = Address(rua, None, "CENTRO", "DOURADOS", "MS", None, rua)
    return Stop(external_id=ext, kind=kind, cliente_id=1, cliente_nome=nome,
                address=a, doc="12345", notes="CACAMBA 5M3")


@pytest.fixture
def rota():
    return VehicleRoute("MB#1", "MB", "MB 1513 — viagem 1", 1, [
        _step(1, "LOC:1", -54.8180, -22.2280),
        _step(2, "LOC:2", -54.7990, -22.2240, "pickup", 30600),
    ], distance_m=12400, duration_s=3600)


# ---- deep links ----------------------------------------------------------
def test_google_maps_link_abre_e_fecha_no_deposito(rota):
    url = google_maps_link(rota, DEPOT)
    q = parse_qs(urlparse(url).query)
    assert q["origin"][0] == "-22.221,-54.806"
    assert q["destination"][0] == "-22.221,-54.806"
    assert q["travelmode"][0] == "driving"


def test_google_maps_link_leva_as_paradas_como_waypoints_na_ordem(rota):
    q = parse_qs(urlparse(google_maps_link(rota, DEPOT)).query)
    assert q["waypoints"][0] == "-22.228,-54.818|-22.224,-54.799"


def test_google_maps_link_de_rota_vazia_nao_explode():
    """Sem paradas não pode haver `waypoints` -- nem vazio. Checar só que a
    URL começa com https deixava a guarda `if route.steps:` sem cobertura:
    removendo a guarda, o teste passava igual (o mutante sobrevive)."""
    url = google_maps_link(VehicleRoute("A#1", "A", "A", 1, []), DEPOT)
    assert url.startswith("https://")
    q = parse_qs(urlparse(url).query)
    assert "waypoints" not in q, q
    assert q["origin"][0] == q["destination"][0] == "-22.221,-54.806"


def test_waze_link_usa_lat_lon_e_navega_direto(rota):
    url = waze_link(rota.steps[0])
    assert "ll=-22.228%2C-54.818" in url or "ll=-22.228,-54.818" in url
    assert "navigate=yes" in url


# ---- CSV -----------------------------------------------------------------
def test_csv_tem_cabecalho_e_uma_linha_por_parada(rota):
    stops = [_stop("LOC:1"), _stop("LOC:2", "CLIENTE Y", kind="pickup")]
    linhas = list(csv.DictReader(io.StringIO(build_csv([rota], stops)), delimiter=";"))
    assert len(linhas) == 2
    assert linhas[0]["veiculo"] == "MB 1513 — viagem 1"
    assert linhas[0]["seq"] == "1"
    assert linhas[0]["cliente"] == "CLIENTE X"
    assert linhas[0]["tipo"] == "Entrega"
    assert linhas[1]["tipo"] == "Coleta"
    assert linhas[0]["chegada"] == "08:00"


def test_csv_sem_rotas_devolve_so_cabecalho():
    texto = build_csv([], [])
    assert texto.strip().count("\n") == 0
    assert "veiculo" in texto


# ---- PDF -----------------------------------------------------------------
def test_pdf_e_um_pdf_valido(rota):
    stops = [_stop("LOC:1"), _stop("LOC:2", "CLIENTE Y", kind="pickup")]
    pdf = build_romaneio_pdf(rota, stops, DEPOT, "Locação", "2026-08-04")
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 1000


def test_pdf_leva_o_deep_link_de_navegacao_por_parada(rota):
    """§6.3 e critério 5 do spec: o link de navegação existe para ser usado.
    Ele era testado unitariamente e não era chamado de lugar nenhum --
    função com teste e sem chamador. O romaneio é onde o motorista está."""
    stops = [_stop("LOC:1"), _stop("LOC:2", "CLIENTE Y", kind="pickup")]
    pdf = build_romaneio_pdf(rota, stops, DEPOT, "Locação", "2026-08-04")
    achatado = pdf.replace(b"\n", b"").replace(b"\r", b"")
    for step in rota.steps:
        # a coordenada de CADA parada, não um link genérico no rodapé
        assert waze_link(step).encode() in achatado, step.stop_external_id


def test_pdf_de_rota_vazia_ainda_gera_documento():
    pdf = build_romaneio_pdf(VehicleRoute("A#1", "A", "A", 1, []), [], DEPOT,
                             "Locação", "2026-08-04")
    assert pdf[:5] == b"%PDF-"
