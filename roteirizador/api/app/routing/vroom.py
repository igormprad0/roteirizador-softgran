from __future__ import annotations

from ..models import (Depot, Solution, Stop, Unassigned, Vehicle,
                      VehicleConfig, VehicleRoute, RouteStep)


class NoGeocodedStops(RuntimeError):
    """Nenhuma parada tem coordenada — não há o que otimizar."""


def expand_trips(fleet: list[VehicleConfig], depot: Depot) -> list[Vehicle]:
    """Cada caminhão físico vira N veículos VROOM, um por viagem, com janelas
    de turno sequenciais. VROOM não modela múltiplas viagens numa rota (§5.3)."""
    out: list[Vehicle] = []
    for cfg in fleet:
        if not cfg.enabled:
            continue
        n = max(1, cfg.trips)
        total = cfg.shift_end_s - cfg.shift_start_s
        passo = total // n
        for i in range(n):
            inicio = cfg.shift_start_s + i * passo
            fim = cfg.shift_end_s if i == n - 1 else inicio + passo
            out.append(Vehicle(
                id=f"{cfg.id}#{i + 1}", config_id=cfg.id,
                label=cfg.label if n == 1 else f"{cfg.label} — viagem {i + 1}",
                trip_index=i + 1, capacity=cfg.capacity,
                shift_start_s=inicio, shift_end_s=fim,
                start=depot.coord, end=depot.coord,
            ))
    return out


def build_payload(stops: list[Stop], vehicles: list[Vehicle]
                  ) -> tuple[dict, dict[int, str], dict[int, Vehicle]]:
    jobs: list[dict] = []
    job_ids: dict[int, str] = {}
    for i, s in enumerate(stops, start=1):
        if s.geo is None or s.geo.confidence == "failed":
            continue
        job: dict = {
            "id": i,
            "location": [s.geo.lon, s.geo.lat],
            "service": s.service_seconds,
            "priority": max(0, min(int(s.priority), 100)),
        }
        if s.kind == "pickup":
            job["pickup"] = [max(1, s.amount)]
        else:
            job["delivery"] = [max(1, s.amount)]
        jobs.append(job)
        job_ids[i] = s.external_id

    if not jobs:
        raise NoGeocodedStops("nenhuma parada geocodificada para otimizar")

    veh_ids: dict[int, Vehicle] = {}
    veh_payload: list[dict] = []
    for i, v in enumerate(vehicles, start=1):
        veh_ids[i] = v
        veh_payload.append({
            "id": i,
            "start": list(v.start),
            "end": list(v.end),
            "capacity": [v.capacity],
            "time_window": [v.shift_start_s, v.shift_end_s],
        })

    return {"jobs": jobs, "vehicles": veh_payload}, job_ids, veh_ids


def parse_solution(body: dict, job_ids: dict[int, str],
                   veh_ids: dict[int, Vehicle], stops: list[Stop]) -> Solution:
    por_ext = {s.external_id: s for s in stops}
    sol = Solution()

    for r in body.get("routes", []):
        v = veh_ids[r["vehicle"]]
        route = VehicleRoute(vehicle_id=v.id, config_id=v.config_id, label=v.label,
                             trip_index=v.trip_index,
                             distance_m=int(r.get("distance", 0)),
                             duration_s=int(r.get("duration", 0)))
        seq = 0
        for step in r.get("steps", []):
            if step.get("type") != "job":
                continue
            seq += 1
            ext = job_ids[step["id"]]
            lon, lat = step["location"]
            load = step.get("load") or [0]
            route.steps.append(RouteStep(
                seq=seq, stop_external_id=ext,
                kind=por_ext[ext].kind if ext in por_ext else "delivery",
                lon=lon, lat=lat,
                arrival_s=int(step.get("arrival", 0)), load_after=int(load[0]),
            ))
        sol.routes.append(route)

    for u in body.get("unassigned", []):
        ext = job_ids.get(u.get("id"))
        if ext:
            sol.unassigned.append(Unassigned(ext, "não coube na frota/janela do dia"))

    atendidas = {s.stop_external_id for r in sol.routes for s in r.steps}
    atendidas |= {u.stop_external_id for u in sol.unassigned}
    for s in stops:
        if s.external_id not in atendidas:
            sol.unassigned.append(
                Unassigned(s.external_id, "sem geocodificação — revisar no mapa"))

    summary = body.get("summary") or {}
    sol.total_distance_m = int(summary.get("distance",
                                           sum(r.distance_m for r in sol.routes)))
    sol.total_duration_s = int(summary.get("duration",
                                           sum(r.duration_s for r in sol.routes)))
    return sol
