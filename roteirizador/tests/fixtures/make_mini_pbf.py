"""Gera tests/fixtures/mini.osm.pbf. Rodar uma vez:
   docker compose run --rm api python tests/fixtures/make_mini_pbf.py
"""
import subprocess
from pathlib import Path

HERE = Path(__file__).parent
XML = HERE / "mini.osm"

XML.write_text("""<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="fixture">
  <node id="1" lat="-22.2200" lon="-54.8100" version="1"/>
  <node id="2" lat="-22.2200" lon="-54.8000" version="1"/>
  <node id="3" lat="-22.2300" lon="-54.8100" version="1"/>
  <node id="4" lat="-22.2300" lon="-54.8000" version="1"/>
  <node id="10" lat="-22.2250" lon="-54.8050" version="1">
    <tag k="place" v="city"/><tag k="name" v="Dourados"/>
  </node>
  <node id="11" lat="-22.2210" lon="-54.8090" version="1">
    <tag k="place" v="suburb"/><tag k="name" v="Centro"/>
  </node>
  <node id="12" lat="-22.2205" lon="-54.8055" version="1">
    <tag k="addr:housenumber" v="1973"/>
    <tag k="addr:street" v="Rua Mato Grosso"/>
    <tag k="addr:city" v="Dourados"/>
  </node>
  <way id="100" version="1">
    <nd ref="1"/><nd ref="2"/>
    <tag k="highway" v="residential"/><tag k="name" v="Rua Mato Grosso"/>
  </way>
  <way id="101" version="1">
    <nd ref="3"/><nd ref="4"/>
    <tag k="highway" v="primary"/><tag k="name" v="Avenida Marcelino Pires"/>
  </way>
  <way id="102" version="1">
    <nd ref="1"/><nd ref="3"/>
    <tag k="highway" v="residential"/><tag k="name" v="Rua Onofre Pereira de Matos"/>
  </way>
</osm>
""", encoding="utf-8")

subprocess.run(["osmium", "cat", str(XML), "-o", str(HERE / "mini.osm.pbf"),
                "--overwrite"], check=True)
print("mini.osm.pbf gerado")
