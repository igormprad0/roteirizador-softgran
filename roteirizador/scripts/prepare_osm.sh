#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../data/osm"

curl -fL -o centro-oeste.osm.pbf \
  https://download.geofabrik.de/south-america/brazil/centro-oeste-latest.osm.pbf

docker run --rm -v "$PWD:/d" -w /d stefda/osmium-tool \
  osmium extract --bbox -58.5,-24.5,-50.8,-17.0 \
                 -o regiao.osm.pbf centro-oeste.osm.pbf --overwrite

OSRM="ghcr.io/project-osrm/osrm-backend:v5.27.1"
docker run --rm -v "$PWD:/data" $OSRM osrm-extract  -p /opt/car.lua /data/regiao.osm.pbf
docker run --rm -v "$PWD:/data" $OSRM osrm-contract /data/regiao.osrm
echo "OSRM pronto: $(ls -la regiao.osrm*| wc -l) artefatos"
