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
                      "percent_km_saved", "monthly_brl_saved", "approximate", "note",
                      "baseline_stops", "optimized_stops"}
    assert c["baseline_km"] > 0
    # `approximate` acompanha a reconstrução do baseline, não o perfil.
    # Neste dia parte das paradas está em caixas de despacho do ERP
    # (DV/RETIRA/CIF) e não em caminhão -- então a ressalva TEM de aparecer.
    # O que nunca pode acontecer é um baseline inviável passar por real.
    assert run["baseline"]["infeasible"] == []
    assert c["approximate"] is run["baseline"]["approximate"]
    if c["approximate"]:
        assert c["note"]


def test_baseline_de_locacao_vem_marcado_como_aproximado():
    run, _ = _otimizar("locacao", LOC_DIA)
    assert run["comparison"]["approximate"] is True
    assert run["comparison"]["note"]


def test_cobertura_da_mesma_frota_e_numero_nao_so_texto():
    """Cobertura tem de existir como NÚMERO, calculada sobre a mesma frota
    dos dois lados -- a UI não consegue renderizar prosa em português.

    Este teste já afirmou `optimized_stops > baseline_stops`. Isso não era
    comportamento, era a alegação de marketing ("+67% de throughput") virada
    assert: o `baseline_stops` de então era artefato de uma regra que
    nenhum despachante segue (encerrar a viagem no primeiro pedido adjacente
    que não cabe, sem olhar o próximo da fila), o que com capacidade 1
    prendia o número em `nº de viagens + 1` qualquer que fosse o dado. Com a
    varredura para a frente, os dois lados podem empatar -- e empatar é um
    resultado, não uma falha. O que o teste prende agora é o mecanismo:
    os dois números existem, saem da MESMA frota, e nenhuma das viagens de
    baseline que produziu `baseline_stops` é inviável."""
    run, _ = _otimizar("locacao", LOC_DIA)
    c = run["comparison"]
    base = run["baseline"]
    assert isinstance(c["baseline_stops"], int) and isinstance(c["optimized_stops"], int)

    # mesma frota dos dois lados: as viagens do baseline saem da lista
    # expandida da frota configurada, e é ela que o VROOM recebe
    frota = client.get("/api/fleet", params={"profile": "locacao"}).json()["fleet"]
    viagens_da_frota = sum(v["trips"] for v in frota if v["enabled"])
    assert len(base["trips"]) <= viagens_da_frota, (
        f"{len(base['trips'])} viagens de baseline para uma frota de "
        f"{viagens_da_frota} viagens -- os dois lados não estão na mesma frota")

    # `baseline_stops` é a contagem dessas viagens, não um número solto
    assert c["baseline_stops"] == len(
        {i for t in base["trips"] for i in t["stop_external_ids"]})
    assert c["optimized_stops"] == sum(len(r["steps"]) for r in run["routes"])
    assert base["infeasible"] == [], base["infeasible"]


@pytest.mark.parametrize("profile,dia", [("locacao", LOC_DIA),
                                         ("entrega_posterior", EP_ULTIMO),
                                         ("entrega_posterior", EP_PICO)])
def test_toda_viagem_do_baseline_caberia_na_frota(profile, dia):
    """A regra de governo deste projeto: *uma viagem de baseline que não
    poderia ter sido executada nunca pode ser reportada como real*.

    Verifica por conta própria -- simula a carga ao longo de cada viagem e
    compara a duração medida contra o turno -- em vez de ler o veredito de
    `service.viagens_inviaveis`. Um portão que só se auto-confirma não
    protege ninguém. Antes da correção, este teste falhava no dia de pico:
    o ERP agrupava 125 paradas sob "ENTREGA DUVIDOSA / BAIXA DV" (caixa de
    baixa administrativa, não caminhão) e o baseline as roteava como uma
    volta contínua de 391 km / 31,6 h -- reportada com approximate=False."""
    run, _ = _otimizar(profile, dia)
    frota = [v for v in client.get("/api/fleet", params={"profile": profile}
                                   ).json()["fleet"] if v["enabled"]]
    assert frota
    cap_max = max(v["capacity"] for v in frota)
    turno_max_h = max(v["shift_end_s"] - v["shift_start_s"] for v in frota) / 3600
    tipo = {s["external_id"]: (s["kind"], s["amount"]) for s in run["stops"]}

    for t in run["baseline"]["trips"]:
        paradas = [tipo[i] for i in t["stop_external_ids"] if i in tipo]
        carga = sum(a for k, a in paradas if k == "delivery")
        assert carga <= cap_max, (
            f"{dia} {t['label']}: sai do depósito com {carga} a bordo, "
            f"capacidade máxima da frota é {cap_max}")
        for k, a in paradas:
            carga += -a if k == "delivery" else a
            assert carga <= cap_max, f"{dia} {t['label']}: carga {carga} > {cap_max}"
        assert t["duration_h"] <= turno_max_h, (
            f"{dia} {t['label']}: {t['duration_h']} h num turno de "
            f"{turno_max_h} h -- nenhum motorista fez essa volta")


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
