from __future__ import annotations
import json
import sqlite3
from pathlib import Path

from ..config import Profile
from ..models import Depot, GeoResult, VehicleConfig

SCHEMA = """
CREATE TABLE IF NOT EXISTS geocode_cache (
  address_key TEXT PRIMARY KEY, lon REAL NOT NULL, lat REAL NOT NULL,
  confidence TEXT NOT NULL, source TEXT NOT NULL, matched_text TEXT,
  score REAL NOT NULL DEFAULT 0, is_manual INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS fleet (
  profile TEXT NOT NULL, id TEXT NOT NULL, label TEXT NOT NULL, placa TEXT,
  capacity INTEGER NOT NULL, trips INTEGER NOT NULL,
  shift_start_s INTEGER NOT NULL, shift_end_s INTEGER NOT NULL,
  enabled INTEGER NOT NULL, erp_id_veiculo INTEGER,
  PRIMARY KEY (profile, id)
);
CREATE TABLE IF NOT EXISTS depot (
  profile TEXT PRIMARY KEY, label TEXT NOT NULL,
  lon REAL NOT NULL, lat REAL NOT NULL, address TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS route_run (
  id INTEGER PRIMARY KEY AUTOINCREMENT, profile TEXT NOT NULL,
  target_date TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')),
  payload_json TEXT NOT NULL
);
"""


class LocalStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def init_schema(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA)

    # ---- geocode ----------------------------------------------------
    def get_geocode(self, address_key: str) -> GeoResult | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM geocode_cache WHERE address_key = ?", (address_key,)
            ).fetchone()
        if row is None:
            return None
        return GeoResult(
            lon=row["lon"], lat=row["lat"],
            confidence=row["confidence"],
            source="manual" if row["is_manual"] else "cache",
            matched_text=row["matched_text"], score=row["score"],
        )

    def put_geocode(self, address_key: str, g: GeoResult) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO geocode_cache
                     (address_key, lon, lat, confidence, source, matched_text, score, is_manual)
                   VALUES (?,?,?,?,?,?,?,0)
                   ON CONFLICT(address_key) DO UPDATE SET
                     lon=excluded.lon, lat=excluded.lat,
                     confidence=excluded.confidence, source=excluded.source,
                     matched_text=excluded.matched_text, score=excluded.score,
                     updated_at=datetime('now')
                   WHERE geocode_cache.is_manual = 0""",
                (address_key, g.lon, g.lat, g.confidence, g.source,
                 g.matched_text, g.score),
            )

    def pin_geocode(self, address_key: str, lon: float, lat: float) -> GeoResult:
        with self._conn() as c:
            c.execute(
                """INSERT INTO geocode_cache
                     (address_key, lon, lat, confidence, source, matched_text, score, is_manual)
                   VALUES (?,?,?, 'high', 'manual', NULL, 100, 1)
                   ON CONFLICT(address_key) DO UPDATE SET
                     lon=excluded.lon, lat=excluded.lat, confidence='high',
                     source='manual', score=100, is_manual=1,
                     updated_at=datetime('now')""",
                (address_key, lon, lat),
            )
        return GeoResult(lon, lat, "high", "manual", None, 100.0)

    # ---- frota e depósito -------------------------------------------
    def get_fleet(self, profile: Profile) -> list[VehicleConfig]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM fleet WHERE profile = ? ORDER BY id", (profile.value,)
            ).fetchall()
        return [
            VehicleConfig(
                id=r["id"], label=r["label"], placa=r["placa"],
                capacity=r["capacity"], trips=r["trips"],
                shift_start_s=r["shift_start_s"], shift_end_s=r["shift_end_s"],
                enabled=bool(r["enabled"]), erp_id_veiculo=r["erp_id_veiculo"],
            )
            for r in rows
        ]

    def put_fleet(self, profile: Profile, fleet: list[VehicleConfig]) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM fleet WHERE profile = ?", (profile.value,))
            c.executemany(
                """INSERT INTO fleet (profile, id, label, placa, capacity, trips,
                       shift_start_s, shift_end_s, enabled, erp_id_veiculo)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [(profile.value, v.id, v.label, v.placa, v.capacity, v.trips,
                  v.shift_start_s, v.shift_end_s, int(v.enabled), v.erp_id_veiculo)
                 for v in fleet],
            )

    def get_depot(self, profile: Profile) -> Depot | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM depot WHERE profile = ?", (profile.value,)).fetchone()
        return None if r is None else Depot(r["label"], r["lon"], r["lat"], r["address"])

    def put_depot(self, profile: Profile, d: Depot) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO depot (profile, label, lon, lat, address) VALUES (?,?,?,?,?)
                   ON CONFLICT(profile) DO UPDATE SET
                     label=excluded.label, lon=excluded.lon,
                     lat=excluded.lat, address=excluded.address""",
                (profile.value, d.label, d.lon, d.lat, d.address),
            )

    # ---- execuções ---------------------------------------------------
    def save_run(self, profile: Profile, target_date: str, payload: dict) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO route_run (profile, target_date, payload_json) VALUES (?,?,?)",
                (profile.value, target_date, json.dumps(payload, ensure_ascii=False)),
            )
            return int(cur.lastrowid)

    def get_run(self, run_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM route_run WHERE id = ?", (run_id,)).fetchone()
        if r is None:
            return None
        return {"id": r["id"], "profile": r["profile"],
                "target_date": r["target_date"], "created_at": r["created_at"],
                **json.loads(r["payload_json"])}
