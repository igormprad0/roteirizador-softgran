#!/usr/bin/env bash
set -euo pipefail
# `data/` inteiro é gitignored e não tem nenhum arquivo versionado, então
# num clone limpo nenhum destes diretórios existe -- o `cd` abaixo falhava e
# o passo 2 do README (o primeiro comando que alguém roda) morria na hora.
# Os três são pontos de montagem do docker-compose: sem eles o Docker cria
# os diretórios como root e o resto quebra mais adiante, de forma mais
# confusa.
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$RAIZ/data/osm" "$RAIZ/data/vroom" "$RAIZ/data/fdb"
cd "$RAIZ/data/osm"

# Git Bash/MSYS reescreve argumentos que parecem ponto de montagem de drive
# (ex.: "/d") para caminhos Windows (ex.: "D:/") antes de o Docker os ver,
# quebrando "-w /d" e "-v $PWD:/d" abaixo. Sem efeito em bash nativo (Linux/Mac).
export MSYS_NO_PATHCONV=1

if [ -s centro-oeste.osm.pbf ]; then
  echo "centro-oeste.osm.pbf já presente, pulando download."
else
  curl -fL -o centro-oeste.osm.pbf \
    https://download.geofabrik.de/south-america/brazil/centro-oeste-latest.osm.pbf
fi

docker run --rm -v "$PWD:/d" -w /d stefda/osmium-tool \
  osmium extract --bbox -58.5,-24.5,-50.8,-17.0 \
                 -o regiao.osm.pbf centro-oeste.osm.pbf --overwrite

OSRM="ghcr.io/project-osrm/osrm-backend:v5.27.1"
docker run --rm -v "$PWD:/data" $OSRM osrm-extract  -p /opt/car.lua /data/regiao.osm.pbf
docker run --rm -v "$PWD:/data" $OSRM osrm-contract /data/regiao.osrm
echo "OSRM pronto: $(ls -la regiao.osrm*| wc -l) artefatos"
