from __future__ import annotations

from ..models import (BaselineResult, BaselineTrip, Comparison, Depot,
                      Solution, Stop)


def measure_baseline(trips: list[BaselineTrip], stops: list[Stop], osrm,
                     depot: Depot, approximate: bool = False,
                     note: str = "") -> BaselineResult:
    por_id = {s.external_id: s for s in stops}
    dist = dur = 0
    usados = 0

    for trip in trips:
        seq = [por_id[i] for i in trip.stop_external_ids
               if i in por_id and por_id[i].geo is not None
               and por_id[i].geo.confidence != "failed"]
        if not seq:
            continue
        usados += 1
        coords = [depot.coord] + [s.geo.coord for s in seq] + [depot.coord]
        leg = osrm.route(coords)
        trip.distance_m = leg.distance_m
        trip.duration_s = leg.duration_s + sum(s.service_seconds for s in seq)
        dist += trip.distance_m
        dur += trip.duration_s

    return BaselineResult(trips=trips, total_distance_m=dist, total_duration_s=dur,
                          vehicles_used=usados, approximate=approximate, note=note)


def compare(solution: Solution, baseline: BaselineResult,
            cost_per_km: float = 3.50, workdays: int = 22) -> Comparison:
    base_km = baseline.total_distance_m / 1000
    otim_km = solution.total_distance_m / 1000
    base_h = baseline.total_duration_s / 3600
    otim_h = solution.total_duration_s / 3600
    km_saved = base_km - otim_km

    return Comparison(
        baseline_km=round(base_km, 1),
        optimized_km=round(otim_km, 1),
        baseline_hours=round(base_h, 1),
        optimized_hours=round(otim_h, 1),
        km_saved=round(km_saved, 1),
        hours_saved=round(base_h - otim_h, 1),
        percent_km_saved=round(km_saved / base_km * 100, 1) if base_km else 0.0,
        monthly_brl_saved=round(km_saved * cost_per_km * workdays, 2),
        approximate=baseline.approximate,
        note=baseline.note,
    )
