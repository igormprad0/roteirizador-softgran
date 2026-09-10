from __future__ import annotations
from urllib.parse import urlencode

from ..models import Depot, RouteStep, VehicleRoute

_GMAPS = "https://www.google.com/maps/dir/?"
_WAZE = "https://www.waze.com/ul?"


def _latlon(lon: float, lat: float) -> str:
    return f"{lat:g},{lon:g}"


def google_maps_link(route: VehicleRoute, depot: Depot) -> str:
    origem = _latlon(depot.lon, depot.lat)
    params = {
        "api": "1",
        "origin": origem,
        "destination": origem,
        "travelmode": "driving",
    }
    if route.steps:
        params["waypoints"] = "|".join(
            _latlon(s.lon, s.lat) for s in sorted(route.steps, key=lambda x: x.seq))
    return _GMAPS + urlencode(params)


def waze_link(step: RouteStep) -> str:
    return _WAZE + urlencode({"ll": _latlon(step.lon, step.lat), "navigate": "yes"})
