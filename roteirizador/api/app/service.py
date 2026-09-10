from __future__ import annotations
from datetime import date

from .config import ImportMode, PROFILES, Profile, get_settings
from .db.firebird import connect
from .db.local import LocalStore
from .erp.base import build_source
from .geo.geocoder import Geocoder
from .models import Depot, Stop, VehicleConfig, VehicleRoute
from .routing.baseline import compare, measure_baseline
from .routing.optimizer import Optimizer
from .routing.osrm import OsrmClient

_APROX_LOCACAO = ("baseline aproximado: o ERP não registra veículo nem ordem "
                  "para locação; usada a ordem de lançamento")


def store() -> LocalStore:
    s = LocalStore(get_settings().local_db)
    s.init_schema()
    return s


def geocoder() -> Geocoder:
    return Geocoder(get_settings().streets_db, store())


def optimizer() -> Optimizer:
    s = get_settings()
    return Optimizer(s.vroom_url, OsrmClient(s.osrm_url), s.solver_timeout_s)


# ---------------------------------------------------------------- importação
def import_stops(profile: Profile, target_date: date,
                 mode: ImportMode) -> tuple[list[Stop], dict]:
    with connect(profile) as conn:
        source = build_source(profile, conn)
        stops = source.fetch(target_date, mode)
        # Coletas vencidas que não couberam no teto do dia. Vai para a tela:
        # dizer "46 paradas" quando 272 ficaram de fora é mentir para o usuário.
        dropped = getattr(source, "dropped_pickups", 0)

    geo = geocoder()
    for s in stops:
        s.address_key, s.geo = geo.geocode(s.address)

    counts = {
        "total": len(stops),
        "pickup_dropped": dropped,
        "delivery": sum(1 for s in stops if s.kind == "delivery"),
        "pickup": sum(1 for s in stops if s.kind == "pickup"),
        "high": sum(1 for s in stops if s.geo and s.geo.confidence == "high"),
        "medium": sum(1 for s in stops if s.geo and s.geo.confidence == "medium"),
        "low": sum(1 for s in stops if s.geo and s.geo.confidence == "low"),
        "failed": sum(1 for s in stops
                      if not s.geo or s.geo.confidence == "failed"),
    }
    return stops, counts


# ---------------------------------------------------------------- serialização
def stop_payload(s: Stop) -> dict:
    a = s.address
    endereco = ", ".join(p for p in [a.logradouro, a.bairro, a.cidade] if p)
    return {
        "external_id": s.external_id, "kind": s.kind,
        "cliente_id": s.cliente_id, "cliente_nome": s.cliente_nome,
        "address": endereco, "address_key": s.address_key,
        "amount": s.amount, "priority": s.priority,
        "days_overdue": s.days_overdue, "doc": s.doc, "notes": s.notes,
        "lon": s.geo.lon if s.geo else None,
        "lat": s.geo.lat if s.geo else None,
        "confidence": s.geo.confidence if s.geo else "failed",
        "source": s.geo.source if s.geo else "none",
    }


def route_payload(r: VehicleRoute) -> dict:
    return {
        "vehicle_id": r.vehicle_id, "config_id": r.config_id, "label": r.label,
        "trip_index": r.trip_index, "geometry": r.geometry,
        "distance_km": round(r.distance_m / 1000, 1),
        "duration_min": round(r.duration_s / 60),
        "steps": [{"seq": s.seq, "stop_external_id": s.stop_external_id,
                   "kind": s.kind, "lon": s.lon, "lat": s.lat,
                   "arrival_s": s.arrival_s, "load_after": s.load_after}
                  for s in sorted(r.steps, key=lambda x: x.seq)],
    }


# ---------------------------------------------------------------- otimização
def optimize(profile: Profile, target_date: date, mode: ImportMode,
             cost_per_km: float = 3.50) -> dict:
    st = store()
    depot = st.get_depot(profile)
    if depot is None:
        raise ValueError("depósito não configurado para este perfil")
    fleet = [v for v in st.get_fleet(profile) if v.enabled]
    if not fleet:
        raise ValueError("nenhum veículo habilitado na frota")

    stops, counts = import_stops(profile, target_date, mode)

    opt = optimizer()
    solution = opt.solve(stops, fleet, depot)

    # O comparativo só é honesto se os dois lados medirem o mesmo trabalho.
    # A frota configurada pode não caber todas as paradas do dia (VROOM
    # devolve `unassigned` para o que não coube) — medir o baseline contra
    # TODAS as paradas enquanto o otimizado só serve um subconjunto infla a
    # economia com o trabalho que simplesmente não foi feito, não com
    # roteirização melhor. Restringe o baseline às paradas que a solução
    # de fato atendeu.
    atendidas_ids = {s.stop_external_id for r in solution.routes for s in r.steps}
    atendidas = [s for s in stops if s.external_id in atendidas_ids]

    with connect(profile) as conn:
        trips = build_source(profile, conn).baseline_order(atendidas)
    aproximado = profile is Profile.LOCACAO
    base = measure_baseline(trips, atendidas, OsrmClient(get_settings().osrm_url), depot,
                            approximate=aproximado,
                            note=_APROX_LOCACAO if aproximado else "")
    comp = compare(solution, base, cost_per_km=cost_per_km)

    payload = {
        "profile": profile.value,
        "mode": mode.value,
        "counts": counts,
        "depot": {"label": depot.label, "lon": depot.lon, "lat": depot.lat,
                  "address": depot.address},
        "stops": [stop_payload(s) for s in stops],
        "routes": [route_payload(r) for r in solution.routes if r.steps],
        "unassigned": [{"stop_external_id": u.stop_external_id, "reason": u.reason}
                       for u in solution.unassigned],
        "totals": {"distance_km": round(solution.total_distance_m / 1000, 1),
                   "duration_h": round(solution.total_duration_s / 3600, 1),
                   "vehicles_used": len([r for r in solution.routes if r.steps]),
                   # A economia acima só vale para estas paradas — não para
                   # o dia inteiro. Quem lê o número precisa saber quanto
                   # ficou de fora.
                   "stops_total": len(stops),
                   "stops_served": len(atendidas),
                   "stops_unassigned": len(stops) - len(atendidas)},
        "comparison": comp.__dict__,
    }
    payload["run_id"] = st.save_run(profile, target_date.isoformat(), payload)
    return payload


def get_run(run_id: int) -> dict | None:
    """Dono canônico de "o que é o run_id de uma execução salva" -- em vez de
    remendar isso na borda HTTP (main.py). `LocalStore.save_run` grava o
    payload_json ANTES de o próprio id existir (só existe depois do INSERT em
    `optimize()` acima), então o JSON persistido nunca traz a chave
    "run_id" -- só o dict devolvido por `optimize()` a tem, por já estar em
    memória quando é atribuída. `LocalStore.get_run` devolve "id" (a mesma
    coisa, vinda da linha). Completar aqui, e não em cada chamador, evita
    remendo espalhado; um `db/local.py` com suporte a update seria a correção
    completa, mas está fora do escopo deste arquivo."""
    run = store().get_run(run_id)
    if run is not None:
        run.setdefault("run_id", run["id"])
    return run
