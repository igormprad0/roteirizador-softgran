# tests/test_api.py
from datetime import date

import pytest
from fastapi.testclient import TestClient

from api.app.main import app

client = TestClient(app)
DIA_LOC = "2026-08-04"
DIA_EP = "2026-08-13"


def test_profiles():
    r = client.get("/api/profiles")
    assert r.status_code == 200
    assert {p["profile"] for p in r.json()} == {"locacao", "entrega_posterior"}


def test_import_com_perfil_invalido_da_422():
    r = client.post("/api/stops/import",
                    json={"profile": "inexistente", "date": DIA_LOC, "mode": "replanejar"})
    assert r.status_code == 422


def test_import_com_data_invalida_da_422():
    r = client.post("/api/stops/import",
                    json={"profile": "locacao", "date": "ontem", "mode": "replanejar"})
    assert r.status_code == 422


def test_run_inexistente_da_404():
    assert client.get("/api/runs/999999").status_code == 404


@pytest.mark.erp
@pytest.mark.slow
def test_import_locacao_devolve_paradas_e_contagens():
    r = client.post("/api/stops/import",
                    json={"profile": "locacao", "date": DIA_LOC, "mode": "replanejar"})
    assert r.status_code == 200
    body = r.json()
    assert body["counts"]["total"] == len(body["stops"])
    assert body["counts"]["total"] > 10
    assert body["counts"]["delivery"] + body["counts"]["pickup"] == body["counts"]["total"]
    assert "pickup_dropped" in body["counts"]        # teto reportado, não engolido
    s = body["stops"][0]
    assert {"external_id", "kind", "cliente_nome", "address", "address_key",
            "lon", "lat", "confidence", "source"} <= set(s)


@pytest.mark.erp
@pytest.mark.slow
def test_taxa_de_geocodificacao_atende_o_criterio_de_sucesso():
    r = client.post("/api/stops/import",
                    json={"profile": "locacao", "date": DIA_LOC, "mode": "replanejar"})
    c = r.json()["counts"]
    bons = c["high"] + c["medium"]
    assert bons / c["total"] >= 0.70, f"geocodificação em {bons}/{c['total']}"


@pytest.mark.erp
@pytest.mark.slow
def test_pin_manual_altera_a_parada_na_proxima_importacao():
    body = client.post("/api/stops/import",
                       json={"profile": "locacao", "date": DIA_LOC,
                             "mode": "replanejar"}).json()
    alvo = body["stops"][0]
    r = client.post("/api/geocode/pin", json={"address_key": alvo["address_key"],
                                              "lon": -54.7000, "lat": -22.1000})
    assert r.status_code == 200 and r.json()["source"] == "manual"

    de_novo = client.post("/api/stops/import",
                          json={"profile": "locacao", "date": DIA_LOC,
                                "mode": "replanejar"}).json()
    mesma = next(s for s in de_novo["stops"]
                 if s["address_key"] == alvo["address_key"])
    assert (mesma["lon"], mesma["lat"]) == (-54.7000, -22.1000)
    assert mesma["source"] == "manual"


@pytest.mark.erp
def test_fleet_sugere_veiculos_do_erp_e_persiste_a_configuracao():
    r = client.get("/api/fleet", params={"profile": "entrega_posterior"})
    assert r.status_code == 200
    assert len(r.json()["suggested"]) > 5           # 37 veículos cadastrados

    novo = [{"id": "CAM1", "label": "Caminhão 1", "placa": "AEY2862",
             "capacity": 12, "trips": 2, "shift_start_s": 25200,
             "shift_end_s": 64800, "enabled": True, "erp_id_veiculo": 5}]
    assert client.put("/api/fleet", json={"profile": "entrega_posterior",
                                          "fleet": novo}).status_code == 200
    guardado = client.get("/api/fleet", params={"profile": "entrega_posterior"}).json()
    assert [v["id"] for v in guardado["fleet"]] == ["CAM1"]
    assert guardado["fleet"][0]["trips"] == 2


@pytest.mark.erp
@pytest.mark.slow
def test_depot_sugerido_vem_do_cadastro_do_estabelecimento():
    r = client.get("/api/depot", params={"profile": "locacao"})
    assert r.status_code == 200
    sug = r.json()["suggested"]
    assert sug is not None and -60 < sug["lon"] < -50


@pytest.mark.erp
@pytest.mark.slow
def test_optimize_devolve_rotas_comparativo_e_persiste_o_run():
    client.put("/api/depot", json={"profile": "locacao", "depot": {
        "label": "Matriz", "lon": -54.8060, "lat": -22.2210,
        "address": "Rua Ponta Porã, 1343"}})
    client.put("/api/fleet", json={"profile": "locacao", "fleet": [
        {"id": "MB", "label": "MB 1513", "placa": "KTD3645", "capacity": 2,
         "trips": 6, "shift_start_s": 25200, "shift_end_s": 68400,
         "enabled": True, "erp_id_veiculo": 2}]})

    r = client.post("/api/optimize", json={"profile": "locacao", "date": DIA_LOC,
                                           "mode": "replanejar", "cost_per_km": 3.5})
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] > 0
    assert len(body["routes"]) >= 1
    assert body["totals"]["distance_km"] > 0
    assert "km_saved" in body["comparison"]
    assert body["comparison"]["approximate"] is True    # baseline de locação

    guardado = client.get(f"/api/runs/{body['run_id']}")
    assert guardado.status_code == 200
    assert guardado.json()["run_id"] == body["run_id"]


@pytest.mark.erp
@pytest.mark.slow
def test_romaneio_e_csv_do_run():
    body = client.post("/api/optimize", json={"profile": "locacao", "date": DIA_LOC,
                                              "mode": "replanejar"}).json()
    veiculo = body["routes"][0]["vehicle_id"]

    pdf = client.get(f"/api/runs/{body['run_id']}/romaneio.pdf",
                     params={"vehicle": veiculo})
    assert pdf.status_code == 200
    assert pdf.headers["content-type"].startswith("application/pdf")
    assert pdf.content[:5] == b"%PDF-"

    csv_r = client.get(f"/api/runs/{body['run_id']}/export.csv")
    assert csv_r.status_code == 200
    assert "veiculo;seq;chegada" in csv_r.text


@pytest.mark.erp
@pytest.mark.slow
def test_romaneio_de_veiculo_inexistente_da_404():
    body = client.post("/api/optimize", json={"profile": "locacao", "date": DIA_LOC,
                                              "mode": "replanejar"}).json()
    r = client.get(f"/api/runs/{body['run_id']}/romaneio.pdf",
                   params={"vehicle": "NAO_EXISTE#9"})
    assert r.status_code == 404
