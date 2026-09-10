from __future__ import annotations

import httpx

from ..models import Depot, Solution, Stop, VehicleConfig
from .osrm import OsrmClient
from .vroom import build_payload, expand_trips, parse_solution


# Margem acrescentada ao `solver_timeout_s` para formar o timeout de LEITURA
# HTTP da chamada ao VROOM. Não é tempo de solver (o VROOM devolve quando
# devolve); é quanto tempo o cliente espera antes de desistir.
_MARGEM_HTTP_S = 160


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
            # `solver_timeout_s` é nome herdado e enganoso: ele nunca
            # limitou o solver, só a leitura HTTP. O dia de pico mediu 10,4 s
            # só dentro do VROOM (12,9 s no total) e este ambiente já mostrou
            # 45-49 s com chamadas empilhadas -- com a margem antiga (+10 s =
            # 30 s) um pico de carga viraria HTTP 400 e um alert() na tela,
            # exatamente quando o README promete "se estiver lento, não
            # travou". Folga bem acima do pior caso observado.
            r = httpx.post(self._url, json=payload,
                           timeout=self._timeout + _MARGEM_HTTP_S)
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
        #
        # Por isso essa chamada NÃO é mais cosmética, e não capturamos nada
        # aqui: uma rota cuja distância não pôde ser medida não pode ser
        # contada como 0 m — isso subestimaria o total do otimizado e
        # inflaria a economia reportada, o pior sentido possível de errar
        # num número que existe para convencer um cliente cético. Se o OSRM
        # falhar, a exceção sobe e o solve() inteiro falha alto, exatamente
        # como measure_baseline já faz (ela também não tem try/except em
        # torno de osrm.route) — os dois lados se comportam da mesma forma.
        por_ext = {s.external_id: s for s in stops}
        for route in solution.routes:
            if not route.steps:
                continue
            coords = [depot.coord] + [(s.lon, s.lat) for s in route.steps] \
                + [depot.coord]
            geo = self._osrm.route(coords)
            route.geometry = geo.polyline
            route.distance_m = geo.distance_m
            route.duration_s = geo.duration_s + sum(
                por_ext[s.stop_external_id].service_seconds
                for s in route.steps if s.stop_external_id in por_ext)

        solution.total_distance_m = sum(r.distance_m for r in solution.routes)
        solution.total_duration_s = sum(r.duration_s for r in solution.routes)
