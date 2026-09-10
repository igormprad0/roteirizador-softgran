from __future__ import annotations

import httpx

from ..models import Depot, Solution, Stop, VehicleConfig
from .osrm import OsrmClient, OsrmError
from .vroom import build_payload, expand_trips, parse_solution


class VroomError(RuntimeError):
    pass


class Optimizer:
    def __init__(self, vroom_url: str, osrm: OsrmClient, timeout_s: int = 20):
        self._url = vroom_url.rstrip("/")
        self._osrm = osrm
        self._timeout = timeout_s

    def solve(self, stops: list[Stop], fleet: list[VehicleConfig],
              depot: Depot) -> Solution:
        vehicles = expand_trips(fleet, depot)
        if not vehicles:
            raise VroomError("nenhum veículo habilitado na frota")

        payload, job_ids, veh_ids = build_payload(stops, vehicles)

        try:
            r = httpx.post(self._url, json=payload, timeout=self._timeout + 10)
        except httpx.HTTPError as exc:
            raise VroomError(f"falha ao chamar o VROOM: {exc}") from exc
        if r.status_code >= 400:
            raise VroomError(f"VROOM {r.status_code}: {r.text[:300]}")

        body = r.json()
        if body.get("code") != 0:
            raise VroomError(f"VROOM code={body.get('code')}: {body.get('error')}")

        solution = parse_solution(body, job_ids, veh_ids, stops)
        self._fill_geometry(solution, depot, stops)
        return solution

    def _fill_geometry(self, solution: Solution, depot: Depot,
                       stops: list[Stop]) -> None:
        # O vroom-express deste projeto roda com `geometry: false` (sem -g),
        # então a rota do VROOM nunca traz distância e sua duração é só uma
        # estimativa vinda da matriz, sem o tempo de serviço. Buscamos a
        # geometria real da sequência já decidida no mesmo OSRM /route que o
        # baseline usa — isso também nos dá a distância e a duração de
        # deslocamento reais, no mesmo motor e nos mesmos termos do baseline.
        por_ext = {s.external_id: s for s in stops}
        for route in solution.routes:
            if not route.steps:
                continue
            coords = [depot.coord] + [(s.lon, s.lat) for s in route.steps] \
                + [depot.coord]
            try:
                geo = self._osrm.route(coords)
            except (OsrmError, httpx.HTTPError):
                # Geometria (e a distância/duração que vêm da mesma chamada)
                # são cosméticas para a validade da solução: sem elas o mapa
                # desenha só os pinos, mas a rota continua válida. Erros fora
                # desses dois tipos sobem — são bugs.
                continue
            route.geometry = geo.polyline
            route.distance_m = geo.distance_m
            route.duration_s = geo.duration_s + sum(
                por_ext[s.stop_external_id].service_seconds
                for s in route.steps if s.stop_external_id in por_ext)

        solution.total_distance_m = sum(r.distance_m for r in solution.routes)
        solution.total_duration_s = sum(r.duration_s for r in solution.routes)
