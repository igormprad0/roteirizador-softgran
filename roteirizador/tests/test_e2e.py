import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from api.app.main import app

client = TestClient(app)
LOC_DIA = "2026-08-04"
EP_PICO = "2024-12-09"
EP_ULTIMO = "2026-08-13"

pytestmark = [pytest.mark.erp, pytest.mark.slow, pytest.mark.stack]


def _otimizar(profile, dia):
    inicio = time.monotonic()
    r = client.post("/api/optimize", json={"profile": profile, "date": dia,
                                           "mode": "replanejar", "cost_per_km": 3.5})
    assert r.status_code == 200, r.text
    return r.json(), time.monotonic() - inicio


# --- critério 1: dia real de cada base em menos de 30 s ---------------------
def test_locacao_roteiriza_em_menos_de_30s():
    run, dt = _otimizar("locacao", LOC_DIA)
    assert dt < 30, f"levou {dt:.1f}s"
    assert len(run["routes"]) >= 1


def test_entrega_posterior_roteiriza_em_menos_de_30s():
    run, dt = _otimizar("entrega_posterior", EP_ULTIMO)
    assert dt < 30, f"levou {dt:.1f}s"


def test_dia_de_pico_215_paradas_roteiriza_em_menos_de_30s():
    run, dt = _otimizar("entrega_posterior", EP_PICO)
    assert dt < 30, f"levou {dt:.1f}s"
    assert run["counts"]["total"] > 190


# --- critério 2: >= 70% em high+medium --------------------------------------
@pytest.mark.parametrize("profile,dia", [("locacao", LOC_DIA),
                                         ("entrega_posterior", EP_ULTIMO)])
def test_taxa_de_geocodificacao(profile, dia):
    c = client.post("/api/stops/import", json={"profile": profile, "date": dia,
                                               "mode": "replanejar"}).json()["counts"]
    bons = c["high"] + c["medium"]
    assert bons / c["total"] >= 0.70, f"{profile}: {bons}/{c['total']}"


# --- critério 3: rotas desenháveis ------------------------------------------
def test_toda_rota_tem_geometria_e_sequencia_continua():
    run, _ = _otimizar("locacao", LOC_DIA)
    for r in run["routes"]:
        assert r["geometry"], f"{r['vehicle_id']} sem geometria"
        assert [s["seq"] for s in r["steps"]] == list(range(1, len(r["steps"]) + 1))
        assert all(s["lat"] and s["lon"] for s in r["steps"])


def test_capacidade_nunca_e_estourada():
    run, _ = _otimizar("locacao", LOC_DIA)
    for r in run["routes"]:
        assert all(s["load_after"] <= 1 for s in r["steps"]), r["vehicle_id"]


def test_nenhuma_parada_some_entre_importacao_e_solucao():
    run, _ = _otimizar("locacao", LOC_DIA)
    atendidas = {s["stop_external_id"] for r in run["routes"] for s in r["steps"]}
    nao = {u["stop_external_id"] for u in run["unassigned"]}
    assert atendidas | nao == {s["external_id"] for s in run["stops"]}


# --- critério 4: comparativo -------------------------------------------------
def test_comparativo_tem_todos_os_numeros():
    run, _ = _otimizar("entrega_posterior", EP_ULTIMO)
    c = run["comparison"]
    assert set(c) >= {"baseline_km", "optimized_km", "km_saved", "hours_saved",
                      "percent_km_saved", "monthly_brl_saved", "approximate", "note"}
    assert c["baseline_km"] > 0
    assert c["approximate"] is False        # baseline real: veículo e hora do ERP


def test_baseline_de_locacao_vem_marcado_como_aproximado():
    run, _ = _otimizar("locacao", LOC_DIA)
    assert run["comparison"]["approximate"] is True
    assert run["comparison"]["note"]


# --- critérios 5 e 6: exportação e aprendizado do cache ----------------------
def test_romaneio_e_csv_de_todos_os_veiculos():
    run, _ = _otimizar("locacao", LOC_DIA)
    for r in run["routes"]:
        pdf = client.get(f"/api/runs/{run['run_id']}/romaneio.pdf",
                         params={"vehicle": r["vehicle_id"]})
        assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"
    assert client.get(f"/api/runs/{run['run_id']}/export.csv").status_code == 200


def test_cache_acelera_a_segunda_importacao():
    corpo = {"profile": "locacao", "date": LOC_DIA, "mode": "replanejar"}
    client.post("/api/stops/import", json=corpo)          # aquece
    t0 = time.monotonic()
    client.post("/api/stops/import", json=corpo)
    assert time.monotonic() - t0 < 5
