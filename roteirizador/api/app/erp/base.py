from __future__ import annotations
from datetime import date
from typing import Protocol

from ..config import ImportMode, Profile
from ..models import BaselineTrip, Stop


class StopSource(Protocol):
    profile: Profile

    def fetch(self, target_date: date, mode: ImportMode) -> list[Stop]: ...

    def baseline_order(self, stops: list[Stop]) -> list[BaselineTrip]: ...
