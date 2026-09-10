import pytest

from api.app.models import (Address, Depot, GeoResult, Stop, VehicleConfig)
from api.app.routing.vroom import (NoGeocodedStops, build_payload,
                                   expand_trips, parse_solution)

DEPOT = Depot("Matriz", -54.8060, -22.2210)


def _stop(sid, kind="delivery", lon=-54.80, lat=-22.22, amount=1,
          priority=0, geocoded=True):
    a = Address("RUA X", "1", None, "DOURADOS", "MS", None, "RUA X, 1")
    s = Stop(external_id=sid, kind=kind, cliente_id=1, cliente_nome="C",
             address=a, amount=amount, priority=priority, service_seconds=600)
    if geocoded:
        s.geo = GeoResult(lon, lat, "high", "street_exact")
    return s


def _cfg(vid="A", trips=1, capacity=5):
    return VehicleConfig(id=vid, label=vid, capacity=capacity, trips=trips,
                         shift_start_s=7 * 3600, shift_end_s=19 * 3600)


# ---- expansão de viagens (§5.3 do spec) ---------------------------------
def test_expand_trips_um_caminhao_uma_viagem():
    vs = expand_trips([_cfg(trips=1)], DEPOT)
    assert len(vs) == 1
    assert vs[0].id == "A#1"
    assert vs[0].start == DEPOT.coord and vs[0].end == DEPOT.coord


def test_expand_trips_divide_a_jornada_entre_as_viagens():
    vs = expand_trips([_cfg(trips=3)], DEPOT)
    assert [v.id for v in vs] == ["A#1", "A#2", "A#3"]
    assert vs[0].shift_start_s == 7 * 3600
    assert vs[-1].shift_end_s == 19 * 3600
    assert vs[0].shift_end_s == vs[1].shift_start_s
    assert all(v.config_id == "A" for v in vs)


def test_expand_trips_ignora_veiculo_desabilitado():
    c = _cfg("B"); c.enabled = False
    assert expand_trips([_cfg("A"), c], DEPOT) == expand_trips([_cfg("A")], DEPOT)


# ---- payload -------------------------------------------------------------
def test_payload_entrega_usa_delivery_e_coleta_usa_pickup():
    stops = [_stop("s1", "delivery", amount=2), _stop("s2", "pickup", amount=3)]
    payload, jobs, _ = build_payload(stops, expand_trips([_cfg()], DEPOT))
    por_ext = {jobs[j["id"]]: j for j in payload["jobs"]}
    assert por_ext["s1"]["delivery"] == [2]
    assert "pickup" not in por_ext["s1"]
    assert por_ext["s2"]["pickup"] == [3]
    assert "delivery" not in por_ext["s2"]


def test_payload_ids_sao_inteiros_e_mapeaveis():
    stops = [_stop("EP:162886"), _stop("LOC:153543")]
    payload, jobs, vehs = build_payload(stops, expand_trips([_cfg()], DEPOT))
    assert all(isinstance(j["id"], int) for j in payload["jobs"])
    assert set(jobs.values()) == {"EP:162886", "LOC:153543"}
    assert all(isinstance(v["id"], int) for v in payload["vehicles"])
    assert set(vehs) == {v["id"] for v in payload["vehicles"]}


def test_payload_leva_capacidade_janela_prioridade_e_servico():
    stops = [_stop("s1", priority=77)]
    payload, _, _ = build_payload(stops, expand_trips([_cfg(capacity=9)], DEPOT))
    v = payload["vehicles"][0]
    assert v["capacity"] == [9]
    assert v["time_window"] == [7 * 3600, 19 * 3600]
    j = payload["jobs"][0]
    assert j["priority"] == 77
    assert j["service"] == 600
    assert j["location"] == [-54.80, -22.22]


def test_payload_descarta_parada_sem_geocodificacao():
    stops = [_stop("bom"), _stop("ruim", geocoded=False)]
    payload, jobs, _ = build_payload(stops, expand_trips([_cfg()], DEPOT))
    assert len(payload["jobs"]) == 1
    assert set(jobs.values()) == {"bom"}


def test_payload_sem_nenhuma_parada_geocodificada_levanta_erro():
    with pytest.raises(NoGeocodedStops):
        build_payload([_stop("x", geocoded=False)], expand_trips([_cfg()], DEPOT))


# ---- leitura da solução ---------------------------------------------------
def _body(job_id, veh_id):
    return {
        "code": 0,
        "routes": [{
            "vehicle": veh_id, "distance": 5400, "duration": 900,
            "steps": [
                {"type": "start", "location": [-54.806, -22.221], "arrival": 25200, "load": [1]},
                {"type": "job", "id": job_id, "location": [-54.80, -22.22],
                 "arrival": 25800, "load": [0]},
                {"type": "end", "location": [-54.806, -22.221], "arrival": 26400, "load": [0]},
            ],
        }],
        "unassigned": [],
        "summary": {"distance": 5400, "duration": 900},
    }


def test_parse_solution_monta_rota_com_sequencia():
    stops = [_stop("s1")]
    payload, jobs, vehs = build_payload(stops, expand_trips([_cfg()], DEPOT))
    jid = payload["jobs"][0]["id"]
    vid = payload["vehicles"][0]["id"]
    sol = parse_solution(_body(jid, vid), jobs, vehs, stops)
    assert len(sol.routes) == 1
    r = sol.routes[0]
    assert r.vehicle_id == "A#1" and r.config_id == "A" and r.trip_index == 1
    assert [s.stop_external_id for s in r.steps] == ["s1"]
    assert r.steps[0].seq == 1
    assert r.steps[0].arrival_s == 25800
    assert r.steps[0].kind == "delivery"
    assert r.distance_m == 5400 and r.duration_s == 900
    assert sol.total_distance_m == 5400 and sol.total_duration_s == 900


def test_parse_solution_registra_nao_atendidas():
    stops = [_stop("s1"), _stop("s2")]
    payload, jobs, vehs = build_payload(stops, expand_trips([_cfg()], DEPOT))
    jid = payload["jobs"][0]["id"]
    outro = payload["jobs"][1]["id"]
    body = _body(jid, payload["vehicles"][0]["id"])
    body["unassigned"] = [{"id": outro, "location": [-54.80, -22.22]}]
    sol = parse_solution(body, jobs, vehs, stops)
    assert [u.stop_external_id for u in sol.unassigned] == [jobs[outro]]


def test_parse_solution_marca_paradas_sem_geocodificacao_como_nao_atendidas():
    stops = [_stop("bom"), _stop("ruim", geocoded=False)]
    payload, jobs, vehs = build_payload(stops, expand_trips([_cfg()], DEPOT))
    body = _body(payload["jobs"][0]["id"], payload["vehicles"][0]["id"])
    sol = parse_solution(body, jobs, vehs, stops)
    assert any(u.stop_external_id == "ruim" and "geocodifica" in u.reason.lower()
               for u in sol.unassigned)
