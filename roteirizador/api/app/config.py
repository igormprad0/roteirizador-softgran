from __future__ import annotations
import os
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path


class Profile(str, Enum):
    LOCACAO = "locacao"
    ENTREGA_POSTERIOR = "entrega_posterior"


class ImportMode(str, Enum):
    PRODUCAO = "producao"
    REPLANEJAR = "replanejar"


@dataclass(frozen=True)
class ProfileConfig:
    profile: Profile
    label: str
    database: str          # caminho do .fdb dentro do container firebird


PROFILES: dict[Profile, ProfileConfig] = {
    Profile.LOCACAO: ProfileConfig(
        Profile.LOCACAO, "Locação de equipamentos", "/db/locacao.fdb"),
    Profile.ENTREGA_POSTERIOR: ProfileConfig(
        Profile.ENTREGA_POSTERIOR, "Entrega posterior", "/db/entrega_posterior.fdb"),
}


@dataclass(frozen=True)
class Settings:
    osrm_url: str
    vroom_url: str
    fdb_host: str
    fb_user: str
    fb_password: str
    local_db: Path
    streets_db: Path
    osm_pbf: Path
    default_city: str = "DOURADOS"
    default_uf: str = "MS"
    solver_timeout_s: int = 20

    def dsn(self, profile: Profile) -> str:
        return f"{self.fdb_host}:{PROFILES[profile].database}"


@lru_cache
def get_settings() -> Settings:
    return Settings(
        osrm_url=os.environ.get("OSRM_URL", "http://osrm:5000"),
        vroom_url=os.environ.get("VROOM_URL", "http://vroom:3000"),
        fdb_host=os.environ.get("FDB_HOST", "firebird"),
        fb_user=os.environ.get("FB_USER", "SYSDBA"),
        fb_password=os.environ.get("FB_PASSWORD", "masterkey"),
        local_db=Path(os.environ.get("LOCAL_DB", "/srv/data/local.db")),
        streets_db=Path(os.environ.get("STREETS_DB", "/srv/data/streets.db")),
        osm_pbf=Path(os.environ.get("OSM_PBF", "/srv/data/osm/regiao.osm.pbf")),
    )
