from __future__ import annotations
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import osmium

from .normalize import expand_abbreviations, strip_accents

SCHEMA = """
DROP TABLE IF EXISTS street;
DROP TABLE IF EXISTS street_fts;
DROP TABLE IF EXISTS housenumber;
DROP TABLE IF EXISTS place;
CREATE TABLE street (
  street_id INTEGER PRIMARY KEY, name TEXT NOT NULL, name_norm TEXT NOT NULL,
  city_norm TEXT NOT NULL DEFAULT '', coords_json TEXT NOT NULL,
  min_lon REAL, min_lat REAL, max_lon REAL, max_lat REAL
);
CREATE VIRTUAL TABLE street_fts USING fts5(
  name_norm, city_norm, street_id UNINDEXED, tokenize='unicode61'
);
CREATE TABLE housenumber (
  street_norm TEXT NOT NULL, city_norm TEXT NOT NULL,
  number TEXT NOT NULL, lon REAL NOT NULL, lat REAL NOT NULL
);
CREATE INDEX ix_hn ON housenumber (street_norm, city_norm, number);
CREATE TABLE place (
  kind TEXT NOT NULL, name_norm TEXT NOT NULL,
  city_norm TEXT NOT NULL DEFAULT '', lon REAL NOT NULL, lat REAL NOT NULL
);
CREATE INDEX ix_place ON place (kind, name_norm);
CREATE INDEX ix_street_norm ON street (name_norm);
"""

_PLACE_KIND = {
    "city": "cidade", "town": "cidade", "village": "cidade", "municipality": "cidade",
    "suburb": "bairro", "neighbourhood": "bairro", "quarter": "bairro",
}


def _norm(s: str) -> str:
    return expand_abbreviations(strip_accents(s).upper()).strip()


@dataclass
class IndexStats:
    streets: int = 0
    housenumbers: int = 0
    places: int = 0


def _inside(bbox, lon: float, lat: float) -> bool:
    if bbox is None:
        return True
    min_lon, min_lat, max_lon, max_lat = bbox
    return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat


class _Handler(osmium.SimpleHandler):
    def __init__(self, con: sqlite3.Connection, bbox):
        super().__init__()
        self.con, self.bbox = con, bbox
        self.stats = IndexStats()
        self._next_id = 1

    def node(self, n):
        if not n.location.valid():
            return
        lon, lat = n.location.lon, n.location.lat
        if not _inside(self.bbox, lon, lat):
            return
        tags = n.tags
        if "addr:housenumber" in tags and "addr:street" in tags:
            self.con.execute(
                "INSERT INTO housenumber (street_norm, city_norm, number, lon, lat)"
                " VALUES (?,?,?,?,?)",
                (_norm(tags["addr:street"]), _norm(tags.get("addr:city", "")),
                 tags["addr:housenumber"].strip(), lon, lat))
            self.stats.housenumbers += 1
        kind = _PLACE_KIND.get(tags.get("place", ""))
        if kind and "name" in tags:
            self.con.execute(
                "INSERT INTO place (kind, name_norm, city_norm, lon, lat) VALUES (?,?,?,?,?)",
                (kind, _norm(tags["name"]), _norm(tags.get("addr:city", "")), lon, lat))
            self.stats.places += 1

    def way(self, w):
        if "highway" not in w.tags or "name" not in w.tags:
            return
        try:
            pts = [[nd.lon, nd.lat] for nd in w.nodes if nd.location.valid()]
        except osmium.InvalidLocationError:
            return
        if len(pts) < 2:
            return
        lons = [p[0] for p in pts]; lats = [p[1] for p in pts]
        mid_lon = (min(lons) + max(lons)) / 2
        mid_lat = (min(lats) + max(lats)) / 2
        if not _inside(self.bbox, mid_lon, mid_lat):
            return
        name = w.tags["name"]
        nn = _norm(name)
        cn = _norm(w.tags.get("addr:city", ""))
        sid = self._next_id
        self._next_id += 1
        self.con.execute(
            "INSERT INTO street (street_id, name, name_norm, city_norm, coords_json,"
            " min_lon, min_lat, max_lon, max_lat) VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, name, nn, cn, json.dumps(pts),
             min(lons), min(lats), max(lons), max(lats)))
        self.con.execute(
            "INSERT INTO street_fts (name_norm, city_norm, street_id) VALUES (?,?,?)",
            (nn, cn, sid))
        self.stats.streets += 1


def build_street_index(pbf_path: Path, out_db: Path,
                       bbox: tuple[float, float, float, float] | None = None) -> IndexStats:
    out_db = Path(out_db)
    out_db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(out_db)
    try:
        con.executescript(SCHEMA)
        h = _Handler(con, bbox)
        h.apply_file(str(pbf_path), locations=True, idx="flex_mem")
        con.commit()
        return h.stats
    finally:
        con.close()
