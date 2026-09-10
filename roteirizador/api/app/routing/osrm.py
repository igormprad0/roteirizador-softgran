from __future__ import annotations
from dataclasses import dataclass

import httpx

from ..models import Coord


class OsrmError(RuntimeError):
    pass


@dataclass
class Matrix:
    durations: list[list[float]]     # segundos
    distances: list[list[float]]     # metros


@dataclass
class RouteGeometry:
    polyline: str                    # polyline5
    distance_m: int
    duration_s: int


def _join(coords: list[Coord]) -> str:
    return ";".join(f"{lon:.6f},{lat:.6f}" for lon, lat in coords)


class OsrmClient:
    def __init__(self, base_url: str, timeout: float = 60.0,
                 max_snap_m: int = 5000):
        self._base = base_url.rstrip("/")
        self._http = httpx.Client(timeout=timeout)
        # Sem isto o OSRM gruda QUALQUER coordenada no nó mais próximo do
        # grafo, sem reclamar. Um lon/lat invertido ou um geocode lixo viraria
        # uma rota plausível e completamente errada. 5 km tolera entrega rural
        # longe de via mapeada e ainda assim recusa o que está noutro estado.
        self._max_snap_m = max_snap_m

    def _radiuses(self, n: int) -> str:
        return ";".join([str(self._max_snap_m)] * n)

    def _get(self, path: str, params: dict) -> dict:
        try:
            r = self._http.get(f"{self._base}{path}", params=params)
        except httpx.HTTPError as exc:
            # httpx só embrulha falhas de STATUS (>=400) abaixo -- erros de
            # TRANSPORTE (conexão recusada, timeout, DNS) escapavam crus daqui
            # e viravam um 500 com stack trace em /api/optimize. OSRM parado,
            # reiniciando ou com a porta errada é a falha operacional mais
            # provável deste stack; precisa virar o mesmo OsrmError que o
            # resto do código já sabe converter num erro limpo para a UI.
            raise OsrmError(f"falha ao conectar ao OSRM ({self._base}): {exc}") from exc
        if r.status_code >= 400:
            raise OsrmError(f"OSRM {r.status_code}: {r.text[:200]}")
        body = r.json()
        if body.get("code") != "Ok":
            raise OsrmError(f"OSRM {body.get('code')}: {body.get('message')}")
        return body

    def table(self, coords: list[Coord]) -> Matrix:
        body = self._get(f"/table/v1/driving/{_join(coords)}",
                         {"annotations": "duration,distance",
                          "radiuses": self._radiuses(len(coords))})
        return Matrix(durations=body["durations"], distances=body["distances"])

    def route(self, coords: list[Coord]) -> RouteGeometry:
        if len(coords) < 2:
            return RouteGeometry("", 0, 0)
        body = self._get(f"/route/v1/driving/{_join(coords)}",
                         {"overview": "full", "geometries": "polyline",
                          "radiuses": self._radiuses(len(coords))})
        route = body["routes"][0]
        return RouteGeometry(route["geometry"], int(route["distance"]),
                             int(route["duration"]))

    def nearest(self, coord: Coord) -> Coord:
        body = self._get(f"/nearest/v1/driving/{coord[0]:.6f},{coord[1]:.6f}",
                         {"number": 1})
        lon, lat = body["waypoints"][0]["location"]
        return (lon, lat)

    def close(self) -> None:
        self._http.close()
