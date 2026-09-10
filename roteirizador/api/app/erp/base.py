from __future__ import annotations
from datetime import date
from typing import Protocol

from ..config import ImportMode, Profile
from ..models import BaselineTrip, Stop, Vehicle


class StopSource(Protocol):
    profile: Profile

    def fetch(self, target_date: date, mode: ImportMode) -> list[Stop]: ...

    def baseline_order(self, stops: list[Stop],
                       vehicles: list[Vehicle]) -> list[BaselineTrip]: ...


def build_source(profile: Profile, conn) -> StopSource:
    from .entrega_posterior import EntregaPosteriorSource
    from .locacao import LocacaoSource

    if profile is Profile.LOCACAO:
        return LocacaoSource(conn)
    if profile is Profile.ENTREGA_POSTERIOR:
        return EntregaPosteriorSource(conn)
    raise ValueError(f"perfil sem StopSource: {profile}")
