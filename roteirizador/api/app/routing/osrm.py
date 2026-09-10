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
    def __init__(self, base_url: str, timeout: float = 60.0):
        self._base = base_url.rstrip("/")
        self._http = httpx.Client(timeout=timeout)

    def _get(self, path: str, params: dict) -> dict:
        r = self._http.get(f"{self._base}{path}", params=params)
        if r.status_code >= 400:
            raise OsrmError(f"OSRM {r.status_code}: {r.text[:200]}")
        body = r.json()
        if body.get("code") != "Ok":
            raise OsrmError(f"OSRM {body.get('code')}: {body.get('message')}")
        return body

    def table(self, coords: list[Coord]) -> Matrix:
        body = self._get(f"/table/v1/driving/{_join(coords)}",
                         {"annotations": "duration,distance"})
        return Matrix(durations=body["durations"], distances=body["distances"])

    def route(self, coords: list[Coord]) -> RouteGeometry:
        if len(coords) < 2:
            return RouteGeometry("", 0, 0)
        body = self._get(f"/route/v1/driving/{_join(coords)}",
                         {"overview": "full", "geometries": "polyline"})
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
