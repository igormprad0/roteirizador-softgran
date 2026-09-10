# tests/test_api.py
import os
from datetime import date

import pytest
from fastapi.testclient import TestClient

from api.app.main import app

client = TestClient(app)
DIA_LOC = "2026-08-04"
DIA_EP = "2026-08-13"


@pytest.fixture(scope="module", autouse=True)
def _local_db_isolado(tmp_path_factory):
    """Este arquivo reconfigura frota/depósito de locação repetidas vezes
    (`PUT /api/fleet`, `PUT /api/depot`) sem restaurar o estado anterior ao
    final de cada teste -- e o fazia contra o MESMO `local.db` real que
    `scripts/seed_demo.py` configura para a demo e que `test_e2e.py` espera
    encontrar intacto. Isso fazia `test_e2e.py::test_capacidade_nunca_e_
    estourada` passar isolado e falhar dentro da suíte inteira, dependendo
    de quem rodou antes -- exatamente o tipo de teste que não deveria
    existir (achado do coordenador, fix round 1).

    Redireciona só `LOCAL_DB` para um arquivo temporário exclusivo deste
    módulo (nunca `STREETS_DB`/`OSM_PBF`/Firebird, que continuam
    apontando para os dados reais -- só o estado de frota/depósito/cache/
    runs precisa de isolamento) e restaura o valor original ao final, para
    que qualquer arquivo rodado depois -- em qualquer ordem -- veja o
    `local.db` exatamente como estava antes deste módulo."""
    from api.app.config import get_settings

    original = os.environ.get("LOCAL_DB")
    caminho = tmp_path_factory.mktemp("test_api_local_db") / "local.db"
    os.environ["LOCAL_DB"] = str(caminho)
    get_settings.cache_clear()
    yield
    if original is None:
        os.environ.pop("LOCAL_DB", None)
    else:
        os.environ["LOCAL_DB"] = original
    get_settings.cache_clear()


def _baseline_km_sobre_todas_as_paradas(profile_str: str, dia: str) -> float:
    """Recalcula o baseline SEM filtrar pelas paradas atendidas -- usado só
    como teto de comparação nos testes, para garantir que o baseline
    reportado por /api/optimize (que filtra para as paradas realmente
    servidas) não pode ser maior nem igual a este."""
    from api.app import service
    from api.app.config import ImportMode, Profile
    from api.app.db.firebird import connect
    from api.app.erp.base import build_source
    from api.app.routing.baseline import measure_baseline
    from api.app.routing.osrm import OsrmClient
    from api.app.routing.vroom import expand_trips

    profile = Profile(profile_str)
    stops, _ = service.import_stops(profile, date.fromisoformat(dia),
                                    ImportMode.REPLANEJAR)
    depot = service.store().get_depot(profile)
    fleet = [v for v in service.store().get_fleet(profile) if v.enabled]
    with connect(profile) as conn:
        trips = build_source(profile, conn).baseline_order(
            stops, expand_trips(fleet, depot))
    base = measure_baseline(trips, stops,
                            OsrmClient(service.get_settings().osrm_url), depot,
                            approximate=True)
    return base.total_distance_m / 1000


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


@pytest.mark.erp
@pytest.mark.slow
def test_baseline_reflete_so_as_paradas_atendidas_quando_frota_nao_cobre_o_dia():
    """Achado real: com a frota de 1 caminhão/capacidade 2 usada nos testes
    acima, o VROOM deixa boa parte das ~38 paradas do dia sem atender. Medir
    o baseline contra TODAS as paradas enquanto o otimizado só serve as que
    coube na frota infla a economia reportada com trabalho que simplesmente
    não foi feito -- não com uma rota melhor. O comparativo só pode citar
    quilometragem do que os dois lados mediram igual."""
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
    totals = body["totals"]

    # a frota acima é deliberadamente pequena para o dia -- se algum dia
    # passar a cobrir tudo, o teste deixou de testar o que pretende testar.
    assert totals["stops_unassigned"] > 0, \
        "frota do teste cobriu o dia inteiro -- ajustar para continuar exercitando o caso parcial"
    assert totals["stops_served"] + totals["stops_unassigned"] == totals["stops_total"]
    assert len(body["unassigned"]) == totals["stops_unassigned"]

    # Achado do revisor: comparar o baseline_km ARREDONDADO (compare()
    # arredonda para 1 casa) contra o teto SEM arredondar passava mesmo com o
    # bug reintroduzido -- 327.4 < 327.419 é verdade só por coincidência de
    # arredondamento, mesmo quando os dois lados mediam as mesmas 38 paradas.
    # Uma margem de metade do teto está bem longe do ruído de arredondamento
    # e bem dentro da diferença real observada (327 vs 94 km, fator ~3.5): se
    # o filtro por atendidas for removido, os dois lados voltam a ficar
    # praticamente iguais e esta margem falha, como deve.
    teto = _baseline_km_sobre_todas_as_paradas("locacao", DIA_LOC)
    baseline_km = body["comparison"]["baseline_km"]
    assert baseline_km < teto * 0.5, (
        f"baseline_km={baseline_km} não está claramente abaixo do teto sobre "
        f"todas as paradas ({teto:.1f}) -- o baseline pode estar medindo "
        f"paradas não atendidas de novo")


@pytest.mark.erp
@pytest.mark.slow
def test_optimize_entrega_posterior_nao_e_aproximado_quando_o_baseline_e_factivel():
    """`approximate=False` só pode ser afirmado quando TODA viagem do
    baseline poderia ter sido executada -- não por o perfil ser entrega
    posterior.

    A versão anterior deste teste travava a afirmação errada: `approximate`
    vinha de `profile is Profile.LOCACAO`, então entrega posterior saía
    sempre False. No dia de pico isso publicou "77% de economia,
    approximate=False" contra uma volta contínua de 125 paradas / 31,6 h de
    uma caixa de despacho do ERP ("ENTREGA DUVIDOSA / BAIXA DV") que nunca
    foi caminhão nenhum. Agora o teste exige as duas coisas juntas: nenhuma
    viagem inviável E `approximate` False -- se a viabilidade cair, o
    esperado é a RESSALVA aparecer, não o número continuar limpo."""
    client.put("/api/depot", json={"profile": "entrega_posterior", "depot": {
        "label": "Matriz", "lon": -54.8060, "lat": -22.2210,
        "address": "Rua Ponta Porã, 1343"}})
    client.put("/api/fleet", json={"profile": "entrega_posterior", "fleet": [
        {"id": "CAM1", "label": "Caminhão 1", "placa": "AEY2862",
         "capacity": 12, "trips": 2, "shift_start_s": 25200,
         "shift_end_s": 64800, "enabled": True, "erp_id_veiculo": 5}]})

    r = client.post("/api/optimize", json={"profile": "entrega_posterior",
                                           "date": DIA_EP, "mode": "replanejar"})
    assert r.status_code == 200
    body = r.json()
    base = body["baseline"]

    # 1. Toda viagem do baseline tem de ser executável. Isto é o invariante,
    #    e vale independentemente de o dia ter ou não veículo registrado.
    assert base["infeasible"] == [], base["infeasible"]

    # 2. `approximate` acompanha a RECONSTRUÇÃO, não o perfil: é False
    #    exatamente quando cada parada foi medida no caminhão que o ERP
    #    registrou (nenhuma viagem "Sem veículo registrado", nenhuma
    #    quebrada por capacidade). Quando não for, a ressalva tem de estar
    #    escrita -- silêncio é o que produziu os 77% do dia de pico.
    reconstruido = any("Sem veículo registrado" in t["label"] or "viagem" in t["label"]
                       for t in base["trips"])
    assert base["approximate"] is reconstruido, base["note"]
    assert body["comparison"]["approximate"] is base["approximate"]
    if base["approximate"]:
        assert base["note"], "baseline reconstruído sem ressalva nenhuma"
    else:
        assert body["comparison"]["note"] == ""


@pytest.mark.erp
@pytest.mark.slow
def test_optimize_com_osrm_inacessivel_da_erro_limpo(monkeypatch):
    """OSRM parado, ainda subindo ou com a porta trocada é a falha
    operacional mais provável deste stack -- container reiniciado no meio do
    dia, por exemplo. A UI precisa mostrar "motor de rotas indisponível",
    não uma stack trace de 500. Aponta OSRM_URL para uma porta morta e
    confirma que /api/optimize devolve um erro limpo (502), não um 500."""
    client.put("/api/depot", json={"profile": "locacao", "depot": {
        "label": "Matriz", "lon": -54.8060, "lat": -22.2210,
        "address": "Rua Ponta Porã, 1343"}})
    client.put("/api/fleet", json={"profile": "locacao", "fleet": [
        {"id": "MB", "label": "MB 1513", "placa": "KTD3645", "capacity": 2,
         "trips": 6, "shift_start_s": 25200, "shift_end_s": 68400,
         "enabled": True, "erp_id_veiculo": 2}]})

    from api.app.config import get_settings
    monkeypatch.setenv("OSRM_URL", "http://127.0.0.1:1")
    get_settings.cache_clear()
    try:
        r = client.post("/api/optimize", json={"profile": "locacao", "date": DIA_LOC,
                                               "mode": "replanejar"})
    finally:
        # Restaura ANTES do teardown do monkeypatch para não vazar a URL
        # quebrada -- via cache -- para os demais testes deste módulo.
        monkeypatch.undo()
        get_settings.cache_clear()

    assert r.status_code == 502
    assert r.status_code != 500


@pytest.mark.erp
@pytest.mark.slow
def test_optimize_chama_o_portao_de_viabilidade_e_marca_o_baseline(monkeypatch):
    """`optimize` TEM de passar o baseline pelo portão de viabilidade.

    `tests/test_viabilidade.py` prova `viagens_inviaveis` como função pura,
    e o teste e2e re-deriva a viabilidade a partir de `baseline.trips` --
    nenhum dos dois nota se a CHAMADA sumir de `service.optimize`. Apagar o
    portão deixava a suíte inteira verde: o defeito de sempre neste
    projeto, um número melhor sem teste vermelho.

    Aqui a fonte é substituída por uma que supõe UMA volta contínua com
    todas as paradas do dia -- exatamente o defeito histórico do dia de
    pico (125 paradas de uma caixa de despacho roteadas como uma rota só) e
    o único jeito de chegar num baseline inviável passando pelo caminho de
    verdade, porque `baseline_order` hoje já quebra os grupos pela
    capacidade. A fonte se declara NÃO aproximada, então o `approximate` do
    payload só pode virar True por obra do portão.
    """
    client.put("/api/depot", json={"profile": "entrega_posterior", "depot": {
        "label": "Matriz", "lon": -54.8060, "lat": -22.2210,
        "address": "Rua Ponta Porã, 1343"}})
    # frota deliberadamente pequena: 6 caçambas é menos do que as paradas
    # que a volta contínua suposta pelo baseline levaria de uma vez
    client.put("/api/fleet", json={"profile": "entrega_posterior", "fleet": [
        {"id": "CAM1", "label": "Caminhão 1", "placa": "AEY2862",
         "capacity": 6, "trips": 4, "shift_start_s": 25200,
         "shift_end_s": 64800, "enabled": True, "erp_id_veiculo": 5}]})

    from api.app import service
    from api.app.erp.base import build_source as _build_real
    from api.app.models import BaselineTrip

    class _UmaVoltaSo:
        """Fonte que devolve as paradas de verdade, mas supõe que todas
        foram feitas numa única volta -- e jura que isso é o registro."""
        baseline_approximate = False
        baseline_note = ""

        def __init__(self, inner):
            self._inner = inner
            self.profile = inner.profile

        def fetch(self, *a, **kw):
            return self._inner.fetch(*a, **kw)

        def baseline_order(self, stops, vehicles):
            return [BaselineTrip("Veículo 11",
                                 [s.external_id for s in stops])]

    monkeypatch.setattr(service, "build_source",
                        lambda profile, conn: _UmaVoltaSo(
                            _build_real(profile, conn)))

    body = client.post("/api/optimize", json={"profile": "entrega_posterior",
                                              "date": DIA_EP,
                                              "mode": "replanejar"}).json()
    base = body["baseline"]
    carga = len(base["trips"][0]["stop_external_ids"])
    assert carga > 6, f"a volta suposta tem {carga} paradas; não testa nada"

    assert base["infeasible"], (
        "o baseline supõe uma volta com mais paradas do que a maior "
        "capacidade da frota e o portão não reclamou -- `optimize` não "
        "está chamando `viagens_inviaveis`")
    assert "capacidade" in base["infeasible"][0]
    # e a ressalva chega até quem lê o número, não fica só no log
    assert base["approximate"] is True
    assert body["comparison"]["approximate"] is True
    assert "NÃO executável" in body["comparison"]["note"]
