import json
import sqlite3
from pathlib import Path

import pytest
from api.app.geo.index_builder import build_street_index

FIXTURE = Path(__file__).parent / "fixtures" / "mini.osm.pbf"


@pytest.fixture(scope="module")
def idx(tmp_path_factory):
    out = tmp_path_factory.mktemp("idx") / "streets.db"
    stats = build_street_index(FIXTURE, out)
    return out, stats


def test_conta_o_que_indexou(idx):
    _, stats = idx
    assert stats.streets == 3
    assert stats.housenumbers == 1
    assert stats.places == 2


def test_ruas_gravadas_com_nome_normalizado(idx):
    db, _ = idx
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    rows = {r["name_norm"]: r for r in c.execute("SELECT * FROM street")}
    assert "RUA MATO GROSSO" in rows
    assert "AVENIDA MARCELINO PIRES" in rows
    assert "RUA ONOFRE PEREIRA DE MATOS" in rows


def test_geometria_em_lon_lat(idx):
    db, _ = idx
    c = sqlite3.connect(db)
    (coords,) = c.execute(
        "SELECT coords_json FROM street WHERE name_norm = 'RUA MATO GROSSO'").fetchone()
    pts = json.loads(coords)
    assert pts == [[-54.81, -22.22], [-54.80, -22.22]]


def test_fts_encontra_por_termo_parcial(idx):
    db, _ = idx
    c = sqlite3.connect(db)
    rows = c.execute(
        "SELECT street_id FROM street_fts WHERE street_fts MATCH ?", ("MATO",)).fetchall()
    assert len(rows) == 1


def test_housenumber_indexado(idx):
    db, _ = idx
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    r = c.execute("SELECT * FROM housenumber WHERE number = '1973'").fetchone()
    assert r["street_norm"] == "RUA MATO GROSSO"
    assert r["city_norm"] == "DOURADOS"
    assert r["lon"] == pytest.approx(-54.8055)


def test_places_bairro_e_cidade(idx):
    db, _ = idx
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    kinds = {r["name_norm"]: r["kind"] for r in c.execute("SELECT * FROM place")}
    assert kinds["DOURADOS"] == "cidade"
    assert kinds["CENTRO"] == "bairro"


def test_bbox_filtra_fora_da_area(tmp_path):
    out = tmp_path / "vazio.db"
    stats = build_street_index(FIXTURE, out, bbox=(-40.0, -10.0, -39.0, -9.0))
    assert stats.streets == 0
