# Roteirizador MVP — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Entregar um roteirizador que importa entregas e coletas do ERP Softgran Empresarial (Firebird), geocodifica endereços brasileiros sujos sem depender de serviço externo, otimiza rotas multi-veículo com carga mista entrega+coleta, e mostra num mapa quanto isso economiza contra a operação atual.

**Architecture:** Quatro containers — OSRM (matriz e geometria), VROOM (solver VRP), Firebird (bases dos clientes, em cópia), e uma API FastAPI que orquestra tudo e serve o front Leaflet. Estado próprio em SQLite; o Firebird do cliente é acessado estritamente em modo leitura. Três interfaces isolam o resto do sistema: `StopSource` (ERP → paradas), `Geocoder` (endereço → coordenada), `Optimizer` (paradas + frota → solução).

**Tech Stack:** Python 3.11, FastAPI, pytest, firebird-driver, pyosmium, rapidfuzz, httpx, reportlab, SQLite FTS5, Leaflet 1.9, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-09-09-roteirizador-mvp-design.md`

## Global Constraints

- **Acesso ao ERP é somente leitura.** `db/firebird.py` rejeita qualquer SQL que não comece com `SELECT` ou `WITH`. Nenhum `INSERT`/`UPDATE`/`DELETE`/`EXECUTE` no Firebird, nunca.
- **Trabalhar em cópias dos `.fdb`.** Os originais em `E:\roteirizador\*.FDB` não são montados em container nem abertos por servidor. Tudo roda sobre `data/fdb/locacao.fdb` e `data/fdb/entrega_posterior.fdb`.
- **Coordenadas são sempre `(lon, lat)`** em toda fronteira interna, alinhado ao OSRM/VROOM/GeoJSON. O único lugar que usa `(lat, lon)` é a UI Leaflet, e a conversão acontece na borda em `web/app.js`.
- **Perfis:** `locacao` e `entrega_posterior`. Enum `Profile`, nunca string solta.
- **Modos de importação:** `producao` (`ENTREGA_PCAB.SITUACAO = 1`) e `replanejar` (`SITUACAO <> 3` na data). Enum `ImportMode`.
- **Excluir sempre `ENTREGA_CAB.TIPO_ENTREGA = 2`** (cliente retira no balcão — não gera parada).
- **Cidade/UF padrão** quando o ERP não informa: `DOURADOS` / `MS`.
- **Todos os testes rodam em container:** `docker compose run --rm api pytest`. Nunca assumir Python no host Windows.
- **Timeout de solver:** VROOM com `-l 20` (20 s). A UI nunca espera mais que isso.
- Commits em português, prefixo convencional (`feat:`, `test:`, `chore:`, `fix:`).

---

## File Structure

```
roteirizador/
  docker-compose.yml            # osrm, vroom, firebird, api
  Dockerfile.api
  requirements.txt
  pytest.ini
  scripts/
    prepare_osm.sh              # download, crop MS, osrm-extract, osrm-contract
    copy_fdb.ps1                # copia os .fdb originais para data/fdb com nome ASCII
  data/
    fdb/                        # cópias dos bancos dos clientes (gitignored)
    osm/                        # .pbf + artefatos .osrm (gitignored)
    local.db                    # SQLite: cache, frota, depósito, rotas (gitignored)
    streets.db                  # SQLite FTS5: índice de ruas (gitignored)
  api/app/
    config.py                   # Settings, Profile, ImportMode, ProfileConfig
    models.py                   # dataclasses do domínio — sem lógica
    db/
      firebird.py               # ErpConnection, read-only enforcement
      local.py                  # LocalStore — schema e CRUD do SQLite
    geo/
      normalize.py              # NormalizedAddress + normalização BR
      index_builder.py          # .pbf -> streets.db
      geocoder.py               # cascata de matching
    erp/
      base.py                   # Protocol StopSource
      entrega_posterior.py
      locacao.py
    routing/
      osrm.py                   # OsrmClient
      vroom.py                  # expand_trips, build_payload, parse_solution
      optimizer.py              # Optimizer.solve
      baseline.py               # measure, compare
    export/
      romaneio.py               # PDF por veículo
      maps_link.py              # deep links Google Maps / Waze
    main.py                     # FastAPI, endpoints, montagem estática
  web/
    index.html  app.js  style.css
  tests/
    conftest.py
    test_normalize.py  test_index_builder.py  test_geocoder.py
    test_firebird.py  test_erp_entrega.py  test_erp_locacao.py
    test_osrm.py  test_vroom.py  test_optimizer.py  test_baseline.py
    test_api.py  test_export.py  test_e2e.py
    fixtures/mini.osm.pbf
```

Responsabilidades: `models.py` não importa nada do projeto (folha da árvore). `geo/` não conhece o ERP. `erp/` não conhece roteirização. `routing/` não conhece o ERP nem geocodificação — recebe `Stop` já com coordenada. `main.py` é a única camada que amarra todos.

---

## Task 1: Scaffold, Docker Compose e preparo do OSM

**Files:**
- Create: `roteirizador/docker-compose.yml`, `roteirizador/Dockerfile.api`, `roteirizador/requirements.txt`, `roteirizador/pytest.ini`, `roteirizador/.gitignore`, `roteirizador/scripts/prepare_osm.sh`, `roteirizador/scripts/copy_fdb.ps1`
- Test: `roteirizador/tests/test_stack.py`

**Interfaces:**
- Consumes: nada
- Produces: serviços `osrm:5000`, `vroom:3000`, `firebird:3050` (exposto em 3051 no host), `api:8000`. Env vars `OSRM_URL`, `VROOM_URL`, `LOCAL_DB`, `STREETS_DB`, `OSM_PBF`, `FDB_HOST`, `FB_USER`, `FB_PASSWORD`.

- [ ] **Step 1: Criar a árvore do projeto e o `.gitignore`**

```bash
mkdir -p roteirizador/{api/app/{db,geo,erp,routing,export},web,tests/fixtures,scripts,data/{fdb,osm}}
cd roteirizador
find api -type d -exec touch {}/__init__.py \;
git init
cat > .gitignore <<'EOF'
data/fdb/
data/osm/
data/*.db
__pycache__/
*.pyc
.pytest_cache/
EOF
```

- [ ] **Step 2: `requirements.txt`**

`firebird-driver` **2.0.3**, não 1.10.6: o driver 1.x não instala sob Python 3.11
quando o pip resolve o `firebird-base` mais novo — `TypeError: issubclass() arg 1
must be a class` em `firebird/base/config.py`. E `firebird-base` precisa de pin
explícito, senão o mesmo problema volta no próximo build limpo.

```
fastapi==0.115.6
uvicorn[standard]==0.34.0
firebird-driver==2.0.3
pydantic==2.10.4
rapidfuzz==3.11.0
osmium==3.7.0
httpx==0.28.1
reportlab==4.2.5
pytest==8.3.4
pytest-asyncio==0.25.0
```

- [ ] **Step 3: `Dockerfile.api`**

`libtommath` é exigida pelo cliente Firebird 3 no Debian bookworm e não vem por padrão — sem ela `firebird-driver` falha ao carregar `libfbclient.so.2`.

```dockerfile
FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
      libfbclient2 libtommath1 fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api /srv/api
COPY web /srv/web
COPY pytest.ini /srv/
COPY tests /srv/tests
ENV PYTHONPATH=/srv
CMD ["uvicorn", "api.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 4: `docker-compose.yml`**

Três valores foram corrigidos durante a execução da Task 1 e já estão certos abaixo:
`vroomvrp/vroom-docker` não existe no Docker Hub (o publicado é `ghcr.io/vroom-project/vroom-docker`),
`firebirdsql/firebird:3.0` não existe (a tag é `3`), e a porta 3050 do host colide com
o Firebird nativo instalado nesta máquina — daí `3051:3050`.

```yaml
services:
  osrm:
    image: ghcr.io/project-osrm/osrm-backend:v5.27.1
    command: osrm-routed --algorithm ch --max-table-size 10000 /data/regiao.osrm
    volumes: ["./data/osm:/data"]
    ports: ["5000:5000"]

  vroom:
    image: ghcr.io/vroom-project/vroom-docker:v1.14.0
    environment:
      VROOM_ROUTER: osrm
      OSRM_HOST: osrm
      OSRM_PORT: "5000"
    volumes: ["./data/vroom:/conf"]
    ports: ["3000:3000"]
    depends_on: [osrm]

  firebird:
    image: firebirdsql/firebird:3
    environment:
      FIREBIRD_ROOT_PASSWORD: masterkey
      FIREBIRD_USER: rotas
      FIREBIRD_PASSWORD: rotas
    volumes: ["./data/fdb:/db"]
    ports: ["3051:3050"]     # 3050 do host já é do Firebird nativo desta máquina

  api:
    build: {context: ., dockerfile: Dockerfile.api}
    environment:
      OSRM_URL: http://osrm:5000
      VROOM_URL: http://vroom:3000
      FDB_HOST: firebird
      FB_USER: SYSDBA
      FB_PASSWORD: masterkey
      LOCAL_DB: /srv/data/local.db
      STREETS_DB: /srv/data/streets.db
      OSM_PBF: /srv/data/osm/regiao.osm.pbf
    volumes: ["./data:/srv/data", "./api:/srv/api", "./web:/srv/web", "./tests:/srv/tests"]
    ports: ["8000:8000"]
    depends_on: [osrm, vroom, firebird]
```

- [ ] **Step 5: `scripts/copy_fdb.ps1` — cópia segura dos bancos**

Roda no host Windows, uma vez. Renomeia para ASCII (o nome com cedilha quebra o `isql` e o mount).

```powershell
$src = "E:\roteirizador"
$dst = "E:\roteirizador\roteirizador\data\fdb"
New-Item -ItemType Directory -Force $dst | Out-Null
Copy-Item "$src\cliente locação.FDB"           "$dst\locacao.fdb"           -Force
Copy-Item "$src\cliente entrega posterior.FDB" "$dst\entrega_posterior.fdb" -Force
Get-ChildItem $dst | Select-Object Name, @{n='GB';e={[math]::Round($_.Length/1GB,2)}}
```

- [ ] **Step 6: Rodar a cópia e conferir**

Run: `powershell -File scripts/copy_fdb.ps1`
Expected: `locacao.fdb ~0.84 GB`, `entrega_posterior.fdb ~3.69 GB`

- [ ] **Step 7: `scripts/prepare_osm.sh` — preparo do grafo (demorado, rodar em background)**

Recorta para Mato Grosso do Sul antes de processar. O bbox cobre Dourados, Campo Grande, Ponta Porã, Maracaju e Itaporã com folga, e reduz o `.pbf` a uma fração do Centro-Oeste.

```bash
#!/usr/bin/env bash
set -euo pipefail

# Git Bash/MSYS reescreve argumentos que parecem caminho POSIX antes do Docker
# vê-los: "-w /d" vira "-w D:/" e o daemon recusa. Sem isto o script morre no
# osmium extract e ainda cria um diretório lixo chamado "osm;D".
export MSYS_NO_PATHCONV=1

cd "$(dirname "$0")/../data/osm"

# ~196 MB: não rebaixar se já veio inteiro numa execução anterior.
if [ ! -s centro-oeste.osm.pbf ]; then
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
```

- [ ] **Step 8: Disparar o preparo em background**

Run: `chmod +x scripts/prepare_osm.sh && nohup ./scripts/prepare_osm.sh > data/osm/prepare.log 2>&1 &`

Leva 20–40 min. **Seguir para a Task 2 enquanto roda.** Acompanhar com `tail -f data/osm/prepare.log`.

- [ ] **Step 9: `pytest.ini`**

```ini
[pytest]
testpaths = tests
markers =
    stack: exige containers osrm/vroom no ar
    erp: exige o container firebird com as bases copiadas
    slow: exige o índice de ruas construído
```

- [ ] **Step 10: Escrever o teste de fumaça da stack**

```python
# tests/test_stack.py
import os, httpx, pytest

OSRM = os.environ.get("OSRM_URL", "http://osrm:5000")
VROOM = os.environ.get("VROOM_URL", "http://vroom:3000")

# Centro de Dourados-MS -> Av. Marcelino Pires, em (lon, lat)
A, B = (-54.8060, -22.2210), (-54.8180, -22.2280)

@pytest.mark.stack
def test_osrm_responde_rota_em_dourados():
    r = httpx.get(f"{OSRM}/route/v1/driving/{A[0]},{A[1]};{B[0]},{B[1]}", timeout=30)
    r.raise_for_status()
    body = r.json()
    assert body["code"] == "Ok"
    assert body["routes"][0]["distance"] > 0

@pytest.mark.stack
def test_vroom_resolve_problema_minimo():
    payload = {
        "vehicles": [{"id": 1, "start": list(A), "end": list(A)}],
        "jobs": [{"id": 1, "location": list(B)}],
    }
    r = httpx.post(VROOM, json=payload, timeout=60)
    r.raise_for_status()
    body = r.json()
    assert body["code"] == 0
    assert len(body["routes"]) == 1
```

- [ ] **Step 11: Rodar o teste — deve falhar enquanto o OSRM não terminou**

Run: `docker compose run --rm api pytest tests/test_stack.py -v -m stack`
Expected: FAIL (conexão recusada) até `prepare_osm.sh` concluir.

- [ ] **Step 12: Subir a stack e rodar de novo**

Run: `docker compose up -d osrm vroom && sleep 15 && docker compose run --rm api pytest tests/test_stack.py -v -m stack`
Expected: 2 passed

- [ ] **Step 13: Verificar que o Firebird abre as duas cópias**

```bash
docker compose up -d firebird && sleep 10
docker compose exec firebird /usr/local/firebird/bin/isql \
  -u SYSDBA -p masterkey /db/locacao.fdb \
  -q -x <<< "SELECT COUNT(*) FROM LOCACAO_PRODUTO;"
```
Expected: `144763`

- [ ] **Step 14: Commit**

```bash
git add -A
git commit -m "chore: scaffold do roteirizador com docker compose, osrm, vroom e firebird"
```

---

## Task 2: Config, modelos de domínio e LocalStore

**Files:**
- Create: `api/app/config.py`, `api/app/models.py`, `api/app/db/local.py`
- Test: `tests/test_local_store.py`

**Interfaces:**
- Consumes: nada do projeto
- Produces:
  - `Profile` (`LOCACAO`, `ENTREGA_POSTERIOR`), `ImportMode` (`PRODUCAO`, `REPLANEJAR`), `Settings`, `get_settings() -> Settings`, `PROFILES: dict[Profile, ProfileConfig]`
  - `Coord = tuple[float, float]  # (lon, lat)`
  - `Address`, `Stop`, `GeoResult`, `VehicleConfig`, `Vehicle`, `Depot`, `RouteStep`, `VehicleRoute`, `Unassigned`, `Solution`, `BaselineTrip`, `BaselineResult`, `Comparison`
  - `LocalStore` com `init_schema`, `get_geocode`, `put_geocode`, `pin_geocode`, `get_fleet`, `put_fleet`, `get_depot`, `put_depot`, `save_run`, `get_run`

- [ ] **Step 1: Escrever `api/app/config.py`**

```python
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
```

- [ ] **Step 2: Escrever `api/app/models.py`**

```python
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

Coord = tuple[float, float]          # SEMPRE (lon, lat)

Confidence = Literal["high", "medium", "low", "failed"]
GeoSource = Literal["manual", "cache", "street_exact", "street_fuzzy",
                    "street_mid", "bairro", "cidade", "none"]
StopKind = Literal["delivery", "pickup"]


@dataclass(frozen=True)
class Address:
    logradouro: str | None
    numero: str | None
    bairro: str | None
    cidade: str | None
    uf: str | None
    cep: str | None
    raw: str


@dataclass
class GeoResult:
    lon: float
    lat: float
    confidence: Confidence
    source: GeoSource
    matched_text: str | None = None
    score: float = 0.0

    @property
    def coord(self) -> Coord:
        return (self.lon, self.lat)


@dataclass
class Stop:
    external_id: str                 # "EP:162886" | "LOC:153543"
    kind: StopKind
    cliente_id: int
    cliente_nome: str
    address: Address
    amount: int = 1
    priority: int = 0                # 0-100
    service_seconds: int = 600
    due_date: date | None = None
    days_overdue: int = 0
    doc: str | None = None
    notes: str = ""
    address_key: str = ""
    geo: GeoResult | None = None
    erp_vehicle_id: int | None = None   # alocação original, usada no baseline
    erp_sequence: int = 0               # ordem original, usada no baseline


@dataclass
class Depot:
    label: str
    lon: float
    lat: float
    address: str = ""

    @property
    def coord(self) -> Coord:
        return (self.lon, self.lat)


@dataclass
class VehicleConfig:
    """Caminhão físico, como o usuário configura na tela."""
    id: str
    label: str
    placa: str | None = None
    capacity: int = 10
    trips: int = 1                   # viagens/dia (§5.3 do spec)
    shift_start_s: int = 7 * 3600
    shift_end_s: int = 18 * 3600
    enabled: bool = True
    erp_id_veiculo: int | None = None


@dataclass
class Vehicle:
    """Uma viagem de um caminhão — é o que vai para o VROOM."""
    id: str                          # "MB1513#2"
    config_id: str                   # "MB1513"
    label: str
    trip_index: int
    capacity: int
    shift_start_s: int
    shift_end_s: int
    start: Coord
    end: Coord


@dataclass
class RouteStep:
    seq: int
    stop_external_id: str
    kind: StopKind
    lon: float
    lat: float
    arrival_s: int
    load_after: int


@dataclass
class VehicleRoute:
    vehicle_id: str
    config_id: str
    label: str
    trip_index: int
    steps: list[RouteStep] = field(default_factory=list)
    distance_m: int = 0
    duration_s: int = 0
    geometry: str = ""               # polyline5 do OSRM


@dataclass
class Unassigned:
    stop_external_id: str
    reason: str


@dataclass
class Solution:
    routes: list[VehicleRoute] = field(default_factory=list)
    unassigned: list[Unassigned] = field(default_factory=list)
    total_distance_m: int = 0
    total_duration_s: int = 0


@dataclass
class BaselineTrip:
    label: str
    stop_external_ids: list[str]


@dataclass
class BaselineResult:
    trips: list[BaselineTrip]
    total_distance_m: int
    total_duration_s: int
    vehicles_used: int
    approximate: bool                # True para locação (§5.4 do spec)
    note: str = ""


@dataclass
class Comparison:
    baseline_km: float
    optimized_km: float
    baseline_hours: float
    optimized_hours: float
    km_saved: float
    hours_saved: float
    percent_km_saved: float
    monthly_brl_saved: float
    approximate: bool
    note: str
```

- [ ] **Step 3: Escrever o teste do LocalStore antes da implementação**

```python
# tests/test_local_store.py
import pytest
from api.app.config import Profile
from api.app.db.local import LocalStore
from api.app.models import Depot, GeoResult, VehicleConfig


@pytest.fixture
def store(tmp_path):
    s = LocalStore(tmp_path / "t.db")
    s.init_schema()
    return s


def test_geocode_roundtrip(store):
    g = GeoResult(-54.80, -22.22, "high", "street_exact", "RUA MATO GROSSO", 100.0)
    store.put_geocode("k1", g)
    got = store.get_geocode("k1")
    assert (got.lon, got.lat) == (-54.80, -22.22)
    assert got.source == "cache"
    assert got.confidence == "high"


def test_geocode_ausente_retorna_none(store):
    assert store.get_geocode("nao-existe") is None


def test_pin_manual_nao_e_sobrescrito_por_put(store):
    store.put_geocode("k1", GeoResult(-54.80, -22.22, "low", "cidade"))
    store.pin_geocode("k1", -54.81, -22.23)
    store.put_geocode("k1", GeoResult(-54.90, -22.90, "high", "street_exact"))
    got = store.get_geocode("k1")
    assert (got.lon, got.lat) == (-54.81, -22.23)
    assert got.source == "manual"
    assert got.confidence == "high"


def test_fleet_roundtrip(store):
    fleet = [VehicleConfig(id="MB1513", label="MB 1513", placa="KTD3645",
                           capacity=1, trips=6, erp_id_veiculo=2)]
    store.put_fleet(Profile.LOCACAO, fleet)
    got = store.get_fleet(Profile.LOCACAO)
    assert len(got) == 1
    assert got[0].trips == 6
    assert got[0].capacity == 1
    assert store.get_fleet(Profile.ENTREGA_POSTERIOR) == []


def test_put_fleet_substitui_o_conjunto_anterior(store):
    store.put_fleet(Profile.LOCACAO, [VehicleConfig(id="A", label="A")])
    store.put_fleet(Profile.LOCACAO, [VehicleConfig(id="B", label="B")])
    assert [v.id for v in store.get_fleet(Profile.LOCACAO)] == ["B"]


def test_depot_roundtrip(store):
    store.put_depot(Profile.LOCACAO, Depot("Matriz", -54.8060, -22.2210, "Rua Ponta Porã, 1343"))
    d = store.get_depot(Profile.LOCACAO)
    assert d.label == "Matriz"
    assert d.coord == (-54.8060, -22.2210)
```

- [ ] **Step 4: Rodar o teste — deve falhar**

Run: `docker compose run --rm api pytest tests/test_local_store.py -v`
Expected: FAIL com `ModuleNotFoundError: No module named 'api.app.db.local'`

- [ ] **Step 5: Implementar `api/app/db/local.py`**

Ponto de atenção: `pin_geocode` grava `is_manual=1`, e `put_geocode` tem que respeitar isso — é por isso que o `INSERT` traz um `WHERE` no `DO UPDATE`.

```python
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
```

- [ ] **Step 6: Rodar os testes**

Run: `docker compose run --rm api pytest tests/test_local_store.py -v`
Expected: 6 passed

- [ ] **Step 7: Commit**

```bash
git add api/app/config.py api/app/models.py api/app/db/local.py tests/test_local_store.py
git commit -m "feat: config, modelos de domínio e persistência local em sqlite"
```

---

## Task 3: Cliente Firebird somente-leitura

**Files:**
- Create: `api/app/db/firebird.py`
- Test: `tests/test_firebird.py`

**Interfaces:**
- Consumes: `Settings`, `Profile`, `PROFILES` de `api.app.config`
- Produces: `ErpConnection(dsn, user, password)` com `query(sql, params=()) -> list[dict]`, `close()`, context manager; `ReadOnlyViolation(RuntimeError)`; `connect(profile: Profile) -> ErpConnection`

- [ ] **Step 1: Escrever o teste**

Os testes marcados `erp` exigem o container `firebird` no ar com as cópias da Task 1. As contagens são as observadas na análise das bases e servem de âncora de regressão.

```python
# tests/test_firebird.py
import pytest
from api.app.config import Profile, get_settings
from api.app.db.firebird import ErpConnection, ReadOnlyViolation, connect


def _conn() -> ErpConnection:
    s = get_settings()
    return ErpConnection(s.dsn(Profile.LOCACAO), s.fb_user, s.fb_password)


@pytest.mark.parametrize("sql", [
    "UPDATE CLIFOR SET NOME = 'x'",
    "DELETE FROM CLIFOR",
    "INSERT INTO CLIFOR (ID) VALUES (1)",
    "  execute procedure FOO",
    "DROP TABLE CLIFOR",
])
def test_recusa_sql_que_nao_seja_leitura(sql):
    c = ErpConnection.__new__(ErpConnection)      # sem abrir conexão
    with pytest.raises(ReadOnlyViolation):
        c._assert_read_only(sql)


def test_aceita_select_e_with():
    c = ErpConnection.__new__(ErpConnection)
    c._assert_read_only("SELECT 1 FROM RDB$DATABASE")
    c._assert_read_only("  with x as (select 1 from rdb$database) select * from x")


@pytest.mark.erp
def test_conecta_e_conta_locacoes():
    with _conn() as c:
        rows = c.query("SELECT COUNT(*) AS N FROM LOCACAO_PRODUTO")
    assert rows[0]["N"] == 144763


@pytest.mark.erp
def test_query_devolve_dicts_com_chaves_maiusculas():
    with _conn() as c:
        rows = c.query("SELECT FIRST 1 ID_SEQUENCIA, DATA_LOCACAO "
                       "FROM LOCACAO_PRODUTO ORDER BY ID_SEQUENCIA")
    assert set(rows[0]) == {"ID_SEQUENCIA", "DATA_LOCACAO"}


@pytest.mark.erp
def test_parametro_posicional():
    with _conn() as c:
        rows = c.query("SELECT COUNT(*) AS N FROM LOCACAO_PRODUTO WHERE SITUACAO = ?", (1,))
    assert rows[0]["N"] == 332


@pytest.mark.erp
def test_acentuacao_vem_correta():
    with _conn() as c:
        rows = c.query("SELECT FIRST 1 NOME FROM CIDADE WHERE NOME LIKE 'ITAPOR%'")
    assert "Ã" in rows[0]["NOME"] or "A" in rows[0]["NOME"]


@pytest.mark.erp
def test_connect_por_perfil_abre_a_base_de_entrega():
    with connect(Profile.ENTREGA_POSTERIOR) as c:
        rows = c.query("SELECT COUNT(*) AS N FROM ENTREGA_PCAB")
    assert rows[0]["N"] == 233041
```

- [ ] **Step 2: Rodar — deve falhar**

Run: `docker compose run --rm api pytest tests/test_firebird.py -v`
Expected: FAIL com `ModuleNotFoundError: No module named 'api.app.db.firebird'`

- [ ] **Step 3: Implementar `api/app/db/firebird.py`**

`charset="WIN1252"` é obrigatório: as bases guardam acentuação em Latin-1 e sem isso `RUA JOÃO ROSA GOES` volta corrompida.

```python
from __future__ import annotations
import re
from typing import Any, Sequence

import firebird.driver as fb

from ..config import Profile, get_settings

_READ_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


class ReadOnlyViolation(RuntimeError):
    """Tentativa de executar SQL que não é leitura contra a base do cliente."""


class ErpConnection:
    def __init__(self, dsn: str, user: str, password: str):
        self._con = fb.connect(dsn, user=user, password=password, charset="WIN1252")

    @staticmethod
    def _assert_read_only(sql: str) -> None:
        if not _READ_ONLY.match(sql):
            raise ReadOnlyViolation(
                f"acesso ao ERP é somente leitura; recusado: {sql.strip()[:80]!r}")

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        self._assert_read_only(sql)
        cur = self._con.cursor()
        try:
            cur.execute(sql, params)
            cols = [d[0].strip() for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            cur.close()

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> "ErpConnection":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def connect(profile: Profile) -> ErpConnection:
    s = get_settings()
    return ErpConnection(s.dsn(profile), s.fb_user, s.fb_password)
```

- [ ] **Step 4: Rodar os testes**

Run: `docker compose run --rm api pytest tests/test_firebird.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add api/app/db/firebird.py tests/test_firebird.py
git commit -m "feat: conexao firebird somente-leitura com bloqueio de sql de escrita"
```

---

## Task 4: Normalização de endereço brasileiro

**Files:**
- Create: `api/app/geo/normalize.py`
- Test: `tests/test_normalize.py`

**Interfaces:**
- Consumes: `Address` de `api.app.models`
- Produces: `NormalizedAddress(street, number, bairro, city, uf, complement, key)`; `strip_accents(s) -> str`; `normalize_city(raw, default_city, default_uf) -> tuple[str, str]`; `expand_abbreviations(s) -> str`; `split_number(s) -> tuple[str, str | None, str | None]`; `normalize_address(addr, default_city="DOURADOS", default_uf="MS") -> NormalizedAddress`

- [ ] **Step 1: Escrever o teste**

Todas as entradas abaixo são amostras reais extraídas de `LOCACAO_PRODUTO.ENDERECO_ENTREGA` e `ENTREGA_CAB.LOGRADOURO`.

```python
# tests/test_normalize.py
import pytest
from api.app.models import Address
from api.app.geo.normalize import (
    NormalizedAddress, expand_abbreviations, normalize_address,
    normalize_city, split_number, strip_accents,
)


def _addr(raw, **kw):
    base = dict(logradouro=raw, numero=None, bairro=None,
                cidade=None, uf=None, cep=None, raw=raw)
    base.update(kw)
    return Address(**base)


def test_strip_accents():
    assert strip_accents("RUA JOÃO ROSA GOES") == "RUA JOAO ROSA GOES"
    assert strip_accents("ITAPORÃ") == "ITAPORA"


@pytest.mark.parametrize("entrada,esperado", [
    ("DOURADOS", ("DOURADOS", "MS")),
    ("DDOS", ("DOURADOS", "MS")),
    ("DORUADOS", ("DOURADOS", "MS")),
    ("DOURADOS - MS", ("DOURADOS", "MS")),
    ("DOURADOS-MS", ("DOURADOS", "MS")),
    ("  dourados  ", ("DOURADOS", "MS")),
    ("CAMPO GRANDE", ("CAMPO GRANDE", "MS")),
    ("ITAPORA", ("ITAPORA", "MS")),
    (None, ("DOURADOS", "MS")),
    ("", ("DOURADOS", "MS")),
])
def test_normalize_city(entrada, esperado):
    assert normalize_city(entrada) == esperado


@pytest.mark.parametrize("entrada,esperado", [
    ("R. JOAO ROSA GOES", "RUA JOAO ROSA GOES"),
    ("AV  WEIMAR G TORRES", "AVENIDA WEIMAR G TORRES"),
    ("ROD DDOS A ITAPORA", "RODOVIA DDOS A ITAPORA"),
    ("AL HECTARES", "ALAMEDA HECTARES"),
    ("TRAV SAO JOSE", "TRAVESSA SAO JOSE"),
    ("PC ANTONIO JOAO", "PRACA ANTONIO JOAO"),
    ("MARCELINO PIRES", "MARCELINO PIRES"),
])
def test_expand_abbreviations(entrada, esperado):
    assert expand_abbreviations(entrada) == esperado


@pytest.mark.parametrize("entrada,rua,numero", [
    ("RUA MATO GROSSO, 1973", "RUA MATO GROSSO", "1973"),
    ("MARCELINO PIRES 4350-A", "MARCELINO PIRES", "4350"),
    ("ALAMEDA HECTARES Nº 665", "ALAMEDA HECTARES", "665"),
    ("ALAMEDA HECTARES N 665", "ALAMEDA HECTARES", "665"),
    ("RUA CURICACA, 1470, ESPLANADA", "RUA CURICACA", "1470"),
    ("SUICA, N/C", "SUICA", None),
    ("HECTARES", "HECTARES", None),
])
def test_split_number(entrada, rua, numero):
    street, num, _ = split_number(entrada)
    assert (street, num) == (rua, numero)


def test_split_number_guarda_quadra_lote_como_complemento():
    street, num, comp = split_number("RUA TARANTO Q 13 LT 06, 105")
    assert street == "RUA TARANTO"
    assert num == "105"
    assert comp is not None and "Q 13" in comp


def test_normalize_endereco_completo():
    n = normalize_address(_addr("RUA ONOFRE PEREIRA DE MATOS,970 CENTRO, 970",
                                bairro="CENTRO", cidade="DDOS"))
    assert n.street.startswith("RUA ONOFRE PEREIRA DE MATOS")
    assert n.number == "970"
    assert n.city == "DOURADOS"
    assert n.uf == "MS"
    assert n.bairro == "CENTRO"


def test_normalize_usa_cidade_padrao_quando_erp_nao_informa():
    n = normalize_address(_addr("RUA PONTA PORA, 160"))
    assert (n.city, n.uf) == ("DOURADOS", "MS")


@pytest.mark.parametrize("raw", [
    "HECTARES", "LOCALIZACAO PORTAL", "JM EVENTO - ROD DDOS A ITAPORA", "", "   ",
])
def test_normalize_endereco_degenerado_nao_explode(raw):
    n = normalize_address(_addr(raw))
    assert isinstance(n, NormalizedAddress)
    assert n.key


def test_key_e_estavel_e_ignora_variacao_de_escrita():
    a = normalize_address(_addr("R. Mato Grosso, 1973", cidade="DOURADOS"))
    b = normalize_address(_addr("RUA  MATO GROSSO 1973", cidade="DDOS"))
    assert a.key == b.key


def test_key_difere_por_numero():
    a = normalize_address(_addr("RUA MATO GROSSO, 1973"))
    b = normalize_address(_addr("RUA MATO GROSSO, 1975"))
    assert a.key != b.key


def test_numero_do_campo_estruturado_tem_precedencia():
    n = normalize_address(_addr("RUA MATO GROSSO", numero="1973"))
    assert n.number == "1973"


def test_uf_do_campo_estruturado_tem_precedencia():
    n = normalize_address(_addr("RUA X, 1", cidade="SAO PAULO", uf="SP"))
    assert n.uf == "SP"
```

- [ ] **Step 2: Rodar — deve falhar**

Run: `docker compose run --rm api pytest tests/test_normalize.py -v`
Expected: FAIL com `ModuleNotFoundError: No module named 'api.app.geo.normalize'`

- [ ] **Step 3: Implementar `api/app/geo/normalize.py`**

```python
from __future__ import annotations
import hashlib
import re
import unicodedata
from dataclasses import dataclass

from ..models import Address

_ABBREV = {
    "R": "RUA", "R.": "RUA",
    "AV": "AVENIDA", "AV.": "AVENIDA", "AVN": "AVENIDA",
    "ROD": "RODOVIA", "ROD.": "RODOVIA",
    "AL": "ALAMEDA", "AL.": "ALAMEDA",
    "TRAV": "TRAVESSA", "TV": "TRAVESSA",
    "PC": "PRACA", "PRC": "PRACA",
    "EST": "ESTRADA", "ESTR": "ESTRADA",
    "LG": "LARGO", "MAR": "MARGINAL",
}

_CIDADE_ALIAS = {
    "DDOS": "DOURADOS", "DORUADOS": "DOURADOS", "DOURDOS": "DOURADOS",
    "CG": "CAMPO GRANDE", "PPORA": "PONTA PORA",
}

_UFS = {"MS", "MT", "GO", "SP", "PR", "DF", "RJ", "MG"}

# "Q 13 LT 06" | "LOTE 06 QUADRA 09" | "QD37" | "LT05"
_QUADRA = re.compile(
    r"\b(?:Q|QD|QUADRA)\s*\.?\s*\d+[A-Z]?\b|\b(?:L|LT|LOTE)\s*\.?\s*\d+[A-Z]?\b")
# ", 1973" | " Nº 665" | " N 665" | " 4350-A" no fim, tolerando sufixo após vírgula
_NUMERO = re.compile(r"(?:,\s*|\s+N[º°.]?\s*|\s+)(\d{1,6})(?:\s*-\s*[A-Z0-9]+)?\s*(?:,[^,]*)?$")
_SEM_NUMERO = re.compile(r",\s*(?:N/C|S/N|SN)\s*$")


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def normalize_city(raw: str | None, default_city: str = "DOURADOS",
                   default_uf: str = "MS") -> tuple[str, str]:
    if not raw or not raw.strip():
        return default_city, default_uf
    s = strip_accents(raw).upper().strip()
    uf = default_uf
    m = re.search(r"[-/\s]\s*([A-Z]{2})\s*$", s)
    if m and m.group(1) in _UFS:
        uf = m.group(1)
        s = s[: m.start()].strip()
    s = re.sub(r"\s+", " ", s).strip(" -/")
    return _CIDADE_ALIAS.get(s, s) or default_city, uf


def expand_abbreviations(s: str) -> str:
    parts = re.sub(r"\s+", " ", s).strip().split(" ")
    if parts and parts[0].upper() in _ABBREV:
        parts[0] = _ABBREV[parts[0].upper()]
    return " ".join(parts)


def split_number(s: str) -> tuple[str, str | None, str | None]:
    """Devolve (rua, numero, complemento). Aceita texto cru; normaliza internamente."""
    s = expand_abbreviations(strip_accents(s or "").upper())

    complement = None
    quadras = _QUADRA.findall(s)
    if quadras:
        complement = " ".join(q.strip() for q in quadras)
        s = _QUADRA.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip().strip(",").strip()

    s = _SEM_NUMERO.sub("", s).strip().strip(",").strip()

    number = None
    m = _NUMERO.search(s)
    if m:
        number = m.group(1)
        s = s[: m.start()].strip().strip(",").strip()
    return s, number, complement


@dataclass(frozen=True)
class NormalizedAddress:
    street: str
    number: str | None
    bairro: str | None
    city: str
    uf: str
    complement: str | None
    key: str


def normalize_address(addr: Address, default_city: str = "DOURADOS",
                      default_uf: str = "MS") -> NormalizedAddress:
    city, uf = normalize_city(addr.cidade, default_city, default_uf)
    if addr.uf and addr.uf.strip():
        uf = strip_accents(addr.uf).upper().strip()[:2] or uf

    street, number, complement = split_number(addr.logradouro or addr.raw or "")

    if addr.numero and str(addr.numero).strip():
        number = re.sub(r"\D", "", str(addr.numero)) or number

    bairro = None
    if addr.bairro and addr.bairro.strip():
        bairro = re.sub(r"\s+", " ", strip_accents(addr.bairro).upper().strip())

    raw_key = f"{street}|{number or ''}|{bairro or ''}|{city}|{uf}"
    key = hashlib.sha1(raw_key.encode("utf-8")).hexdigest()[:20]
    return NormalizedAddress(street, number, bairro, city, uf, complement, key)
```

- [ ] **Step 4: Rodar os testes**

Run: `docker compose run --rm api pytest tests/test_normalize.py -v`
Expected: 34 passed

- [ ] **Step 5: Commit**

```bash
git add api/app/geo/normalize.py tests/test_normalize.py
git commit -m "feat: normalizacao de endereco brasileiro com amostras reais das bases"
```

---

## Task 5: Índice de ruas a partir do `.pbf`

**Files:**
- Create: `api/app/geo/index_builder.py`
- Test: `tests/test_index_builder.py`, `tests/fixtures/make_mini_pbf.py`

**Interfaces:**
- Consumes: `strip_accents`, `expand_abbreviations` de `api.app.geo.normalize`
- Produces: `IndexStats(streets, housenumbers, places)`; `build_street_index(pbf_path: Path, out_db: Path, bbox: tuple[float,float,float,float] | None = None) -> IndexStats`; schema de `streets.db` consumido pela Task 6:
  - `street(street_id INTEGER PK, name TEXT, name_norm TEXT, city_norm TEXT, coords_json TEXT, min_lon, min_lat, max_lon, max_lat)` — `coords_json` é `[[lon,lat], ...]`
  - `street_fts(name_norm, city_norm, street_id UNINDEXED)` — FTS5
  - `housenumber(street_norm TEXT, city_norm TEXT, number TEXT, lon REAL, lat REAL)` + índice `(street_norm, city_norm, number)`
  - `place(kind TEXT, name_norm TEXT, city_norm TEXT, lon REAL, lat REAL)` — `kind` ∈ `bairro`, `cidade`

- [ ] **Step 1: Gerador da fixture `.pbf` mínima**

Não versionar um `.pbf` binário. Gerar em XML e converter com `osmium`, que já está no compose como imagem avulsa.

```python
# tests/fixtures/make_mini_pbf.py
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
```

Se `osmium` não estiver no container `api`, gerar pelo container avulso:
`docker run --rm -v "$PWD/tests/fixtures:/d" -w /d stefda/osmium-tool osmium cat mini.osm -o mini.osm.pbf --overwrite`

- [ ] **Step 2: Escrever o teste**

```python
# tests/test_index_builder.py
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
```

- [ ] **Step 3: Rodar — deve falhar**

Run: `docker compose run --rm api pytest tests/test_index_builder.py -v`
Expected: FAIL com `ModuleNotFoundError: No module named 'api.app.geo.index_builder'`

- [ ] **Step 4: Implementar `api/app/geo/index_builder.py`**

`locations=True` com `idx="flex_mem"` é o que permite ler a geometria das `way` sem uma segunda passada. Para o `.pbf` recortado de MS isso cabe em memória confortavelmente.

```python
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
```

- [ ] **Step 5: Rodar os testes**

Run: `docker compose run --rm api pytest tests/test_index_builder.py -v`
Expected: 7 passed

- [ ] **Step 6: Construir o índice real (demora, rodar em background)**

```bash
docker compose run --rm -d api python -c "
from pathlib import Path
from api.app.geo.index_builder import build_street_index
print(build_street_index(Path('/srv/data/osm/regiao.osm.pbf'), Path('/srv/data/streets.db')))
"
```
Expected ao terminar: `IndexStats(streets=...>50000, housenumbers=...>1000, places=...>500)`

- [ ] **Step 7: Commit**

```bash
git add api/app/geo/index_builder.py tests/test_index_builder.py tests/fixtures/
git commit -m "feat: indice de ruas offline construido a partir do pbf do osm"
```

---

## Task 6: Geocoder em cascata

**Files:**
- Create: `api/app/geo/geocoder.py`
- Test: `tests/test_geocoder.py`

**Interfaces:**
- Consumes: `LocalStore` (Task 2), `normalize_address`/`NormalizedAddress` (Task 4), schema de `streets.db` (Task 5), `Address`/`GeoResult` (Task 2)
- Produces: `Geocoder(index_db: Path, store: LocalStore, fuzzy_high: int = 88, fuzzy_low: int = 75)` com `geocode(addr: Address) -> tuple[str, GeoResult]` (devolve `(address_key, resultado)`) e `pin(address_key: str, lon: float, lat: float) -> GeoResult`

- [ ] **Step 1: Escrever o teste**

```python
# tests/test_geocoder.py
import json
import sqlite3
import pytest

from api.app.db.local import LocalStore
from api.app.geo.geocoder import Geocoder
from api.app.models import Address

RUA_MG = [[-54.8100, -22.2200], [-54.8000, -22.2200]]


@pytest.fixture
def geo(tmp_path):
    idx = tmp_path / "streets.db"
    c = sqlite3.connect(idx)
    c.executescript("""
      CREATE TABLE street (street_id INTEGER PRIMARY KEY, name TEXT, name_norm TEXT,
        city_norm TEXT, coords_json TEXT, min_lon REAL, min_lat REAL,
        max_lon REAL, max_lat REAL);
      CREATE VIRTUAL TABLE street_fts USING fts5(name_norm, city_norm,
        street_id UNINDEXED, tokenize='unicode61');
      CREATE TABLE housenumber (street_norm TEXT, city_norm TEXT, number TEXT,
        lon REAL, lat REAL);
      CREATE TABLE place (kind TEXT, name_norm TEXT, city_norm TEXT, lon REAL, lat REAL);
    """)
    # city_norm dos ways vem VAZIO de propósito: no índice real só 55 de 55.063
    # linhas têm addr:city. Um fixture que preenchesse isso testaria fantasia.
    c.execute("INSERT INTO street VALUES (1,'Rua Mato Grosso','RUA MATO GROSSO',"
              "'',?,-54.81,-22.22,-54.80,-22.22)", (json.dumps(RUA_MG),))
    c.execute("INSERT INTO street_fts VALUES ('RUA MATO GROSSO','',1)")
    # Marcelino Pires em dois segmentos distantes, como no índice real (20 lá).
    c.execute("INSERT INTO street VALUES (2,'Avenida Marcelino Pires',"
              "'AVENIDA MARCELINO PIRES','','[[-54.82,-22.23],[-54.79,-22.23]]',"
              "-54.82,-22.23,-54.79,-22.23)")
    c.execute("INSERT INTO street_fts VALUES ('AVENIDA MARCELINO PIRES','',2)")
    c.execute("INSERT INTO street VALUES (3,'Avenida Marcelino Pires',"
              "'AVENIDA MARCELINO PIRES','','[[-54.83,-22.28],[-54.80,-22.28]]',"
              "-54.83,-22.28,-54.80,-22.28)")
    c.execute("INSERT INTO street_fts VALUES ('AVENIDA MARCELINO PIRES','',3)")
    c.execute("INSERT INTO housenumber VALUES ('RUA MATO GROSSO','DOURADOS','1973',"
              "-54.8055,-22.2205)")
    c.execute("INSERT INTO housenumber VALUES ('RUA MATO GROSSO','DOURADOS','2500',"
              "-54.8010,-22.2200)")
    c.execute("INSERT INTO place VALUES ('bairro','CENTRO','DOURADOS',-54.8090,-22.2210)")
    c.execute("INSERT INTO place VALUES ('bairro','AGUA BOA','DOURADOS',-54.8150,-22.2800)")
    c.execute("INSERT INTO place VALUES ('cidade','DOURADOS','',-54.8050,-22.2250)")
    c.commit(); c.close()

    store = LocalStore(tmp_path / "local.db"); store.init_schema()
    return Geocoder(idx, store)


def _a(logradouro, **kw):
    base = dict(logradouro=logradouro, numero=None, bairro=None, cidade="DOURADOS",
                uf="MS", cep=None, raw=logradouro)
    base.update(kw)
    return Address(**base)


def test_housenumber_exato_tem_prioridade(geo):
    _, r = geo.geocode(_a("RUA MATO GROSSO, 1973"))
    assert r.source == "street_exact"
    assert r.confidence == "high"
    assert (r.lon, r.lat) == pytest.approx((-54.8055, -22.2205))


def test_rua_exata_com_numero_desconhecido_interpola(geo):
    _, r = geo.geocode(_a("RUA MATO GROSSO, 500"))
    assert r.source in ("street_exact", "street_fuzzy")
    assert -54.8100 <= r.lon <= -54.8000
    assert r.lat == pytest.approx(-22.2200)


def test_fuzzy_corrige_erro_de_digitacao(geo):
    _, r = geo.geocode(_a("RUA MATO GROSO, 500"))
    assert r.source in ("street_fuzzy", "street_exact")
    assert r.confidence == "medium"


def test_sem_numero_cai_no_ponto_medio_da_via(geo):
    _, r = geo.geocode(_a("AVENIDA MARCELINO PIRES"))
    assert r.source in ("street_mid", "street_fuzzy", "street_exact")
    assert r.lat == pytest.approx(-22.23)


def test_rua_desconhecida_cai_no_bairro(geo):
    _, r = geo.geocode(_a("RUA QUE NAO EXISTE EM LUGAR NENHUM", bairro="CENTRO"))
    assert r.source == "bairro"
    assert r.confidence == "low"
    assert (r.lon, r.lat) == pytest.approx((-54.8090, -22.2210))


def test_sem_rua_e_sem_bairro_cai_na_cidade(geo):
    _, r = geo.geocode(_a("HECTARES"))
    assert r.source == "cidade"
    assert r.confidence == "low"


def test_cidade_desconhecida_resulta_em_failed(geo):
    _, r = geo.geocode(_a("QUALQUER COISA", cidade="CIDADE INEXISTENTE", bairro=None))
    assert r.source == "none"
    assert r.confidence == "failed"


def test_segunda_chamada_vem_do_cache(geo):
    k1, r1 = geo.geocode(_a("RUA MATO GROSSO, 1973"))
    k2, r2 = geo.geocode(_a("R. Mato Grosso 1973"))
    assert k1 == k2
    assert r2.source == "cache"
    assert (r2.lon, r2.lat) == (r1.lon, r1.lat)


def test_pin_manual_vence_o_cache_e_o_matching(geo):
    key, _ = geo.geocode(_a("RUA MATO GROSSO, 1973"))
    geo.pin(key, -54.7000, -22.1000)
    _, r = geo.geocode(_a("RUA MATO GROSSO, 1973"))
    assert r.source == "manual"
    assert (r.lon, r.lat) == (-54.7000, -22.1000)


def test_endereco_vazio_nao_explode(geo):
    _, r = geo.geocode(_a(""))
    assert r.confidence in ("low", "failed")


# ---- escolha de segmento e interpolação (§ dados reais da Task 5) ----------

def test_escolhe_o_segmento_da_via_mais_perto_do_bairro(geo):
    """A Marcelino Pires real são 20 segmentos somando 9,3 km. Pegar o errado
    erra por quilômetros; o bairro é o que desempata."""
    _, norte = geo.geocode(_a("AVENIDA MARCELINO PIRES", bairro="CENTRO"))
    _, sul = geo.geocode(_a("AVENIDA MARCELINO PIRES", bairro="AGUA BOA"))
    assert norte.lat == pytest.approx(-22.23)
    assert sul.lat == pytest.approx(-22.28)


def test_sem_bairro_conhecido_ancora_na_cidade(geo):
    _, r = geo.geocode(_a("AVENIDA MARCELINO PIRES", bairro="BAIRRO INEXISTENTE"))
    # centroide de DOURADOS é -22.2250, mais perto do segmento norte
    assert r.lat == pytest.approx(-22.23)


def test_usa_o_numero_conhecido_mais_proximo(geo):
    """Sem o número exato, o vizinho mais próximo posiciona muito melhor que
    o ponto médio — e MAX(number) não serve porque 94% das ruas não têm número."""
    _, r = geo.geocode(_a("RUA MATO GROSSO, 2400"))
    assert (r.lon, r.lat) == pytest.approx((-54.8010, -22.2200))   # vizinho 2500
    _, perto_do_1973 = geo.geocode(_a("RUA MATO GROSSO, 1980"))
    assert (perto_do_1973.lon, perto_do_1973.lat) == pytest.approx((-54.8055, -22.2205))


def test_via_sem_nenhum_numero_cai_no_meio_do_segmento_escolhido(geo):
    _, r = geo.geocode(_a("AVENIDA MARCELINO PIRES, 500", bairro="AGUA BOA"))
    assert r.lat == pytest.approx(-22.28)      # segmento certo, não a via inteira


def test_nao_usa_city_norm_do_way_para_filtrar(geo):
    """city_norm dos ways está vazio no índice real; se o geocoder filtrasse
    por ele, não acharia rua nenhuma."""
    _, r = geo.geocode(_a("RUA MATO GROSSO, 1973", cidade="DOURADOS"))
    assert r.confidence in ("high", "medium")
    assert r.source != "cidade"


# ---- escopo de cidade em TODA consulta -------------------------------------
# O fixture abaixo replica as colisões reais do índice: mesmo nome de rua,
# mesmo número de porta e mesmo nome de bairro existindo em outro município.

@pytest.fixture
def geo_colisao(tmp_path):
    idx = tmp_path / "streets.db"
    c = sqlite3.connect(idx)
    c.executescript("""
      CREATE TABLE street (street_id INTEGER PRIMARY KEY, name TEXT, name_norm TEXT,
        city_norm TEXT, coords_json TEXT, min_lon REAL, min_lat REAL,
        max_lon REAL, max_lat REAL);
      CREATE VIRTUAL TABLE street_fts USING fts5(name_norm, city_norm,
        street_id UNINDEXED, tokenize='unicode61');
      CREATE TABLE housenumber (street_norm TEXT, city_norm TEXT, number TEXT,
        lon REAL, lat REAL);
      CREATE TABLE place (kind TEXT, name_norm TEXT, city_norm TEXT, lon REAL, lat REAL);
    """)
    # Mesma rua em Dourados (-54.81/-22.22) e em Campo Grande (-54.61/-20.46).
    c.execute("INSERT INTO street VALUES (1,'Rua Mato Grosso','RUA MATO GROSSO','',"
              "'[[-54.8100,-22.2200],[-54.8000,-22.2200]]',-54.81,-22.22,-54.80,-22.22)")
    c.execute("INSERT INTO street_fts VALUES ('RUA MATO GROSSO','',1)")
    c.execute("INSERT INTO street VALUES (2,'Rua Mato Grosso','RUA MATO GROSSO','',"
              "'[[-54.6100,-20.4600],[-54.6000,-20.4600]]',-54.61,-20.46,-54.60,-20.46)")
    c.execute("INSERT INTO street_fts VALUES ('RUA MATO GROSSO','',2)")
    # Rua numerada: todos os tokens são genéricos ou dígitos.
    c.execute("INSERT INTO street VALUES (3,'Alameda 5','ALAMEDA 5','',"
              "'[[-54.8300,-22.2400],[-54.8200,-22.2400]]',-54.83,-22.24,-54.82,-22.24)")
    c.execute("INSERT INTO street_fts VALUES ('ALAMEDA 5','',3)")
    # Mesmo número de porta nas duas cidades; só o de CG tem city_norm.
    c.execute("INSERT INTO housenumber VALUES ('RUA MATO GROSSO','DOURADOS','1973',"
              "-54.8055,-22.2205)")
    c.execute("INSERT INTO housenumber VALUES ('RUA MATO GROSSO','CAMPO GRANDE','1973',"
              "-54.6055,-20.4605)")
    # CENTRO existe nas duas; o de CG vem primeiro na varredura.
    c.execute("INSERT INTO place VALUES ('bairro','CENTRO','',-54.6133,-20.4614)")
    c.execute("INSERT INTO place VALUES ('bairro','CENTRO','',-54.8112,-22.2279)")
    c.execute("INSERT INTO place VALUES ('cidade','DOURADOS','',-54.8050,-22.2250)")
    c.execute("INSERT INTO place VALUES ('cidade','CAMPO GRANDE','',-54.6133,-20.4614)")
    c.commit(); c.close()
    store = LocalStore(tmp_path / "local.db"); store.init_schema()
    return Geocoder(idx, store)


def test_numero_de_porta_de_outra_cidade_nao_e_usado(geo_colisao):
    """O caso real: 'RUA MATO GROSSO, 1973' em Dourados resolvia a 145 km."""
    _, r = geo_colisao.geocode(_a("RUA MATO GROSSO, 1973", cidade="DOURADOS"))
    assert r.lat == pytest.approx(-22.2205, abs=0.02)
    assert r.lon == pytest.approx(-54.8055, abs=0.02)


def test_bairro_homonimo_de_outra_cidade_nao_e_usado(geo_colisao):
    """CENTRO existe em 10 municípios do extrato real."""
    _, r = geo_colisao.geocode(_a("RUA QUE NAO EXISTE", cidade="DOURADOS",
                                  bairro="CENTRO"))
    assert r.source == "bairro"
    assert r.lat == pytest.approx(-22.2279, abs=0.02)


def test_segmento_de_outra_cidade_nao_e_escolhido(geo_colisao):
    _, r = geo_colisao.geocode(_a("RUA MATO GROSSO", cidade="DOURADOS"))
    assert r.lat == pytest.approx(-22.22, abs=0.05)


def test_rua_numerada_continua_encontravel(geo_colisao):
    """RUA 6, ALAMEDA 1..9 e afins são convenção de loteamento brasileiro.
    O filtro de palavras genéricas não pode torná-las inencontráveis."""
    _, r = geo_colisao.geocode(_a("ALAMEDA 5", cidade="DOURADOS"))
    assert r.source in ("street_exact", "street_fuzzy", "street_mid")
    assert r.lat == pytest.approx(-22.24, abs=0.02)


def test_candidato_longe_demais_da_cidade_e_recusado(geo_colisao):
    """Sem nada plausível perto, cai no centroide da cidade — não vai buscar
    a 200 km e devolver `medium` como se tivesse acertado."""
    _, r = geo_colisao.geocode(_a("RUA MATO GROSSO, 1973", cidade="CAMPO GRANDE"))
    assert r.lat == pytest.approx(-20.46, abs=0.05)
```

- [ ] **Step 2: Rodar — deve falhar**

Run: `docker compose run --rm api pytest tests/test_geocoder.py -v`
Expected: FAIL com `ModuleNotFoundError: No module named 'api.app.geo.geocoder'`

- [ ] **Step 3: Implementar `api/app/geo/geocoder.py`**

### O que o índice real da Task 5 obrigou a mudar aqui

O plano original desta task presumia dados que não existem. Medições sobre o `streets.db` construído:

| medida | valor real | consequência |
|---|---|---|
| `housenumber` em Dourados | **111** | o degrau `street_exact` quase nunca dispara |
| ruas urbanas de Dourados com algum número | **~6%** | `MAX(number)` é `NULL` para ~94% das ruas |
| `street.city_norm` preenchido | **55 de 55.063** | filtrar por `city_norm` é inútil — ways do OSM não têm `addr:city` |
| segmentos da AV. MARCELINO PIRES | **20 ways, 9,3 km** | um "ponto médio da rua" erra por quilômetros |

Três correções decorrem disso, e nenhuma é opcional:

1. **Escopo de cidade por geometria, não por `city_norm`.** Filtrar por `city_norm` deixaria o geocoder casar uma rua homônima em Campo Grande. Em vez disso, pontuar os candidatos pela distância ao centroide da cidade, que vem da tabela `place`.
2. **Escolha de segmento pelo bairro.** Uma rua é várias linhas de `street` com o mesmo `name_norm`. Escolher entre elas pelo centroide do bairro — quando o ERP informa bairro — troca um erro de 9,3 km por um de algumas centenas de metros. É a mudança de maior impacto nesta task.
3. **Interpolação só quando há dado.** Havendo números na via, usar o **mais próximo** do procurado em vez de `number/MAX`. Não havendo, ir ao ponto médio **do segmento escolhido**, não da rua inteira.

Nada disso conserta a esparsidade do OSM — conserta o comportamento diante dela. Uma parada com a rua certa e posição aproximada dentro dela ordena uma rota corretamente; uma parada a 5 km inverte a sequência.

### Para a locação, o ganho é cobertura, não distância

Com o baseline já corrigido para respeitar a capacidade, os números finais medidos são:

| | locação (04/08) | entrega posterior (13/08) |
|---|---|---|
| paradas atendidas pelo otimizador | **25** de 38 | 17 de 18 |
| paradas que a ordem de lançamento encaixa na mesma frota | **15** | — (baseline real do ERP) |
| economia em distância | 0,8% | **29,6%** |
| baseline | aproximado | real (veículo e hora do ERP) |

São dois casos de valor diferentes, e apresentar os dois como "economia de km" desserve o principal:

- **Entrega posterior:** ganho clássico de roteirização. 29,6% menos quilômetro, baseline real do ERP. O número fala sozinho.
- **Locação:** a distância por parada é praticamente a mesma (0,8%). O ganho é **throughput** — 25 entregas por dia contra 15, com os mesmos dois caminhões, +67%. Para um poliguindaste de capacidade 1 a rota é quase determinada pela física; o que o otimizador melhora é *quantas* cabem no dia, não *quanto* se roda.

Hoje esses 15 existem apenas dentro de `comparison.note`, em prosa. A tela mostraria "0,8% · R$ 40,81/mês" e enterraria o argumento.

**Correção:** `comparison` ganha `baseline_stops` e `optimized_stops` como números, e a tela mostra cobertura ao lado de distância — sempre os dois, deixando o dado falar por cliente em vez de escolher a narrativa no código.

### O baseline tem de ser fisicamente possível (medido na Task 15)

Com a frota realista (2 poliguindastes, capacidade 1, 14 viagens/dia) a economia medida na locação caiu para **0,6%** — contra 36,8% com a frota-placeholder menor da Task 13. Investigado até a causa: **o baseline é infactível.**

`LocacaoSource.baseline_order` particiona a ordem de lançamento em blocos de ~12 paradas e `measure_baseline` roteia cada bloco como uma volta contínua. Isso pressupõe um caminhão que sai com 12 caçambas. Um poliguindaste carrega **uma**, e volta ao depósito a cada uma ou duas paradas — restrição física, não escolha do otimizador.

Ou seja: comparava-se uma rota otimizada que **obedece** a capacidade contra um baseline que **a ignora**. O otimizado é obrigado a fazer idas-e-vindas curtas, formato caro por natureza; o baseline encadeia doze paradas sem nunca voltar. O resultado subestima o ganho tanto quanto o bug anterior o superestimava.

**Regra, a mesma que vale para o resto da comparação:** os dois lados obedecem às mesmas leis físicas e usam a mesma frota. A única diferença permitida é *quais paradas andam juntas e em que ordem* — nunca *se a rota é executável*.

Implementação: o baseline da locação usa a **mesma lista de veículos expandida** que o otimizador (`expand_trips`), atribuindo as paradas em ordem de lançamento e respeitando a capacidade de cada viagem exatamente como o solver precisa respeitar. Entrega posterior não muda: ali o baseline é o veículo e a hora que o ERP registrou, factível por construção.

O número que sair disso é o número. Pode ser maior ou menor que 0,6% — não ajustar frota nem particionamento para chegar a um valor desejado.

### Proximidade entra ANTES do score, não depois

Depois da regra do número: locação 86%, entrega posterior travada em 56%. As que restam falham todas pelo mesmo motivo estrutural — **o nome é escolhido por score difuso e só então a geografia é consultada**. Uma homônima distante com score coincidentemente maior vence, e a rua certa, ali em Dourados, nunca chega a ser avaliada:

- `RUA VEREADOR AGUIAR FERREIRA DE SOUZA` existe em Dourados, **empata** no score com uma via distante e perde por ordenação arbitrária de conjunto
- `SANTOS DUMONT` e `IPANEMA` perdem para homônimas distantes de score um pouco maior
- `ALAMEDA DAS HORTENCIAS` é pior: o teto de 400 candidatos do FTS é consumido pela palavra `DAS`, e os segmentos corretos nem são recuperados

Duas correções:

1. **Particionar os candidatos por proximidade antes de pontuar.** Entre os que estão dentro do raio da cidade, vence o melhor score. Só se não houver nenhum perto é que os distantes são considerados — e aí a confiança cai. Isso resolve empate, resolve homônima e é o comportamento que o resto do módulo já pressupõe.
2. **Não gastar o teto do FTS com conectivos.** `DAS`, `DOS`, `DE`, `DA`, `DO`, `E` não distinguem nada e enchem os 400 candidatos com ruído. Removê-los dos termos de busca — mantendo o fallback já existente para quando a filtragem esvaziar tudo.

### O número é separador, não terminador (medido, não suposto)

Medição com cache limpo depois do escopo de cidade: **63% locação, 56% entrega**, zero apontando para cidade errada. Consultando o índice pelos endereços que falharam, quase todos **existem em Dourados**:

`RUA ONOFRE PEREIRA DE MATOS` · `AVENIDA CORONEL PONCIANO` · `RUA BARAO DO RIO BRANCO` · `RUA SANTOS DUMONT` · `ALAMEDA DAS HORTENCIAS` · `AVENIDA PRESIDENTE VARGAS` · `RUA AURORA AUGUSTA DE MATTOS` · `RUA ANTONIO EMILIO DE FIGUEIREDO` · `RUA IPANEMA` · `RUA PROJETADA`

Só `ALAGOAS` e `VEREADOR AGUIAR DE SOUZA` estão de fato ausentes. Ou seja: **não é lacuna de cobertura do OSM, é falha de casamento.** O texto que sobra depois do número fica no nome da rua e afunda o score:

| endereço cru do ERP | rua que o índice tem |
|---|---|
| `RUA ONOFRE PEREIRA DE MATOS,970 CENTRO, 970` | RUA ONOFRE PEREIRA DE MATOS |
| `BARAO DO RIO BRANCO 395 JARDIM TROPICAL` | RUA BARAO DO RIO BRANCO |
| `CORONEL PONCIANO 1425 NOVA DOURADOS` | AVENIDA CORONEL PONCIANO |
| `SANTOS DUMONT MARMITARIA, 1361` | RUA SANTOS DUMONT |
| `Aurora Augusta de Matos, 3600 (fundos Ecov` | RUA AURORA AUGUSTA DE MATTOS |
| `Rua Antonio E. de Figueiredo, 2280 Esq. Ca` | RUA ANTONIO EMILIO DE FIGUEIREDO |
| `RUA PROJETADA A, 285
C VALDEREZ OLIVEIRA` | RUA PROJETADA |
| `AV: PRESIDENTE VARGAS, LT1 Q 18(4948)` | AVENIDA PRESIDENTE VARGAS |
| `ALAMENDA DAS HORTENCIAS 225, 225` | ALAMEDA DAS HORTENCIAS |

Regra em `split_number`: **o primeiro número isolado encerra o nome da rua.** Tudo depois dele é complemento, nunca nome. Junto com isso: normalizar `
`/`	` para espaço, tratar `AV:` como `AV.`, e descartar trecho entre parênteses no fim.

Os 49 testes de `test_normalize.py` continuam valendo sem alteração — a regra vale a partir do primeiro número, e casos como `RUA 13 DE MAIO 500` ou `25 DE MARCO 100` já provam que dígito dentro do nome não pode disparar o corte sozinho.

### O escopo de cidade vale para TODA consulta, não só para a escolha de segmento

Primeira tentativa desta task aplicou o escopo geográfico só em `_pick_segment` e deixou as outras quatro consultas varrendo o extrato inteiro. O resultado mediu 97% e 89% — e era ilusório: contava como acerto endereços resolvidos em outro município. Medido de novo com verificação de plausibilidade geográfica, caiu para **53% e 33%**. Casos reais:

- `"RUA MATO GROSSO, 1973"`, cidade DOURADOS → resolveu a **145 km** de Dourados
- `"AV. PRESIDENTE VARGAS, 3095"`, cidade DOURADOS → número de porta encontrado em **Iguatemi, 160 km**
- `bairro CENTRO` existe em **10 municípios** do extrato; `LIMIT 1` devolvia o de Campo Grande, 190 km fora

Dois dados que mudam o desenho:

- **`housenumber.city_norm` está 64% preenchido** (1.299 de 2.027) — diferente de `street.city_norm`, ele é utilizável e estava sendo ignorado.
- **`place.city_norm` para bairro está preenchido em 1 de 1.388 linhas** — inútil, igual ao de street. Bairro precisa de desempate geométrico.

Regra única, aplicada em todas as consultas: **nenhum candidato a mais de `CITY_RADIUS_DEG` do centroide da cidade é aceito**, e entre os que sobram vence o mais próximo. `0.30°` ≈ 33 km nesta latitude — cobre o município com folga e recusa a cidade vizinha. Onde `city_norm` existir e for utilizável (housenumber), usá-lo primeiro e cair no critério geométrico só como reserva.

E o filtro `_TIPO_VIA` não pode ser tudo-ou-nada: ele torna **18 ruas reais de Dourados inencontráveis** — `RUA 6`, `RUA 8`, `ALAMEDA 1` a `ALAMEDA 9`, `RUA MARGINAL CD 01` (cujo único token distintivo, `MARGINAL`, está na própria lista). Nomes numerados são convenção corrente em loteamento brasileiro. Quando a filtragem esvaziar o conjunto de termos, usar os termos originais.

```python
from __future__ import annotations
import json
import re
import sqlite3
from pathlib import Path

from rapidfuzz import fuzz, process

from ..db.local import LocalStore
from ..models import Address, GeoResult
from .normalize import NormalizedAddress, normalize_address

_FTS_SAFE = re.compile(r"[^A-Z0-9 ]")


class Geocoder:
    def __init__(self, index_db: Path, store: LocalStore,
                 fuzzy_high: int = 88, fuzzy_low: int = 75):
        self._db = Path(index_db)
        self._store = store
        self._hi, self._lo = fuzzy_high, fuzzy_low

    # ------------------------------------------------------------------
    def geocode(self, addr: Address, default_city: str = "DOURADOS",
                default_uf: str = "MS") -> tuple[str, GeoResult]:
        n = normalize_address(addr, default_city, default_uf)

        cached = self._store.get_geocode(n.key)
        if cached is not None:
            return n.key, cached

        con = sqlite3.connect(self._db)
        con.row_factory = sqlite3.Row
        try:
            result = (self._by_housenumber(con, n)
                      or self._by_street(con, n)
                      or self._by_bairro(con, n)
                      or self._by_cidade(con, n)
                      or GeoResult(0.0, 0.0, "failed", "none"))
        finally:
            con.close()

        if result.confidence != "failed":
            self._store.put_geocode(n.key, result)
        return n.key, result

    def pin(self, address_key: str, lon: float, lat: float) -> GeoResult:
        return self._store.pin_geocode(address_key, lon, lat)

    # ------------------------------------------------------------------
    # -- âncoras geográficas -------------------------------------------
    def _place(self, con, kind: str, name_norm: str) -> tuple[float, float] | None:
        r = con.execute(
            "SELECT lon, lat FROM place WHERE kind = ? AND name_norm = ? LIMIT 1",
            (kind, name_norm)).fetchone()
        return (r["lon"], r["lat"]) if r else None

    @staticmethod
    def _dist2(a: tuple[float, float], b: tuple[float, float]) -> float:
        """Distância ao quadrado em graus. Só serve para ordenar candidatos —
        não converter para metros, a escala de lon/lat difere."""
        return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2

    def _by_housenumber(self, con, n: NormalizedAddress) -> GeoResult | None:
        """Casamento exato de número. Raro em Dourados (111 números no índice
        inteiro), mas quando acerta é a melhor posição que existe."""
        if not n.number or not n.street:
            return None
        r = con.execute(
            "SELECT lon, lat FROM housenumber WHERE street_norm = ? AND number = ?"
            " LIMIT 1", (n.street, n.number)).fetchone()
        if r is None:
            return None
        return GeoResult(r["lon"], r["lat"], "high", "street_exact", n.street, 100.0)

    def _candidates(self, con, n: NormalizedAddress) -> list[sqlite3.Row]:
        terms = [t for t in _FTS_SAFE.sub(" ", n.street).split() if len(t) > 2]
        if not terms:
            return []
        query = " OR ".join(terms)
        ids = [row["street_id"] for row in con.execute(
            "SELECT street_id FROM street_fts WHERE street_fts MATCH ? LIMIT 400",
            (query,))]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return con.execute(
            f"SELECT * FROM street WHERE street_id IN ({marks})", ids).fetchall()

    def _by_street(self, con, n: NormalizedAddress) -> GeoResult | None:
        """Casa o nome da via, escolhe entre os segmentos homônimos pelo bairro
        (ou pela cidade), e só então posiciona o ponto dentro do segmento."""
        if not n.street:
            return None
        rows = self._candidates(con, n)
        if not rows:
            return None

        nomes = {r["name_norm"] for r in rows}
        best = process.extractOne(n.street, list(nomes), scorer=fuzz.WRatio)
        if best is None or best[1] < self._lo:
            return None
        name, score = best[0], float(best[1])

        segmentos = [r for r in rows if r["name_norm"] == name]
        # Âncora: bairro se o ERP informou e o índice conhece, senão a cidade.
        # `street.city_norm` NÃO serve — está vazio em 99,9% das linhas.
        ancora = (self._place(con, "bairro", n.bairro) if n.bairro else None) \
            or self._place(con, "cidade", n.city)
        row = self._pick_segment(segmentos, ancora)
        pts = json.loads(row["coords_json"])

        if score >= self._hi and n.street == name:
            confidence, source = "high", "street_exact"
        elif score >= self._hi:
            confidence, source = "medium", "street_fuzzy"
        elif n.number:
            confidence, source = "medium", "street_fuzzy"
        else:
            confidence, source = "medium", "street_mid"

        lon, lat = self._point_on_street(con, pts, name, n)
        return GeoResult(lon, lat, confidence, source, name, score)

    @staticmethod
    def _pick_segment(segmentos: list, ancora: tuple[float, float] | None):
        """Uma via é várias linhas de `street`. A Marcelino Pires são 20
        segmentos somando 9,3 km — escolher o errado erra por quilômetros."""
        if len(segmentos) == 1 or ancora is None:
            return segmentos[0]
        def centro(r):
            return ((r["min_lon"] + r["max_lon"]) / 2,
                    (r["min_lat"] + r["max_lat"]) / 2)
        return min(segmentos, key=lambda r: Geocoder._dist2(centro(r), ancora))

    def _point_on_street(self, con, pts, name_norm: str,
                         n: NormalizedAddress) -> tuple[float, float]:
        """Com número e com vizinhos conhecidos: usa o número mais próximo.
        Sem dado de número: ponto médio DO SEGMENTO escolhido, não da via."""
        meio = tuple(pts[len(pts) // 2])
        if not n.number:
            return meio
        try:
            alvo = int(n.number)
        except ValueError:
            return meio

        vizinhos = con.execute(
            "SELECT number, lon, lat FROM housenumber WHERE street_norm = ?",
            (name_norm,)).fetchall()
        candidatos = []
        for v in vizinhos:
            try:
                candidatos.append((abs(int(v["number"]) - alvo), v["lon"], v["lat"]))
            except (TypeError, ValueError):
                continue
        if candidatos:
            _, lon, lat = min(candidatos, key=lambda c: c[0])
            return (lon, lat)
        return meio

    def _by_bairro(self, con, n: NormalizedAddress) -> GeoResult | None:
        if not n.bairro:
            return None
        r = con.execute(
            "SELECT lon, lat FROM place WHERE kind = 'bairro' AND name_norm = ?"
            " LIMIT 1", (n.bairro,)).fetchone()
        if r is None:
            return None
        return GeoResult(r["lon"], r["lat"], "low", "bairro", n.bairro, 50.0)

    def _by_cidade(self, con, n: NormalizedAddress) -> GeoResult | None:
        r = con.execute(
            "SELECT lon, lat FROM place WHERE kind = 'cidade' AND name_norm = ? LIMIT 1",
            (n.city,)).fetchone()
        if r is None:
            return None
        return GeoResult(r["lon"], r["lat"], "low", "cidade", n.city, 25.0)
```

- [ ] **Step 4: Rodar os testes**

Run: `docker compose run --rm api pytest tests/test_geocoder.py -v`
Expected: 15 passed

- [ ] **Step 5: Medir a taxa real contra as duas bases**

Os testes provam o comportamento; este passo mede se ele resolve o problema. É o número do critério de sucesso nº 2 (≥ 70% em `high`+`medium`).

**A medição conta apenas resultados geograficamente plausíveis.** A primeira versão desta task mediu 97% e 89% contando como acerto endereços resolvidos a 145 e 190 km da cidade certa. Um `medium` apontando para outro município não é acerto parcial — é a falha mais cara do sistema, porque parece certa e reordena a rota inteira. A caixa de Dourados abaixo (~30×25 km) é generosa de propósito: o que cair fora dela está errado, ponto.

```bash
docker compose run --rm api python -c "
from datetime import date
from pathlib import Path
from api.app.db.firebird import connect
from api.app.db.local import LocalStore
from api.app.erp.base import build_source
from api.app.config import ImportMode, Profile
from api.app.geo.geocoder import Geocoder
from collections import Counter

store = LocalStore(Path('/srv/data/local.db')); store.init_schema()
geo = Geocoder(Path('/srv/data/streets.db'), store)

# Plausível = caiu perto do centroide da cidade que o ERP informou para AQUELA
# parada. Uma caixa fixa em Dourados contaria como erro uma entrega correta em
# Itaporã ou Maracaju — e os dois clientes atendem essas cidades de verdade.
import sqlite3
from api.app.geo.normalize import normalize_address
_idx = sqlite3.connect('/srv/data/streets.db'); _idx.row_factory = sqlite3.Row
def centro_da_cidade(nome):
    r = _idx.execute("SELECT lon, lat FROM place WHERE kind='cidade'"
                     " AND name_norm=? LIMIT 1", (nome,)).fetchone()
    return (r['lon'], r['lat']) if r else None

def plausivel(g, addr, raio_graus=0.30):
    alvo = centro_da_cidade(normalize_address(addr).city)
    if alvo is None:
        return None            # cidade desconhecida no índice: não julgar
    return (g.lon - alvo[0]) ** 2 + (g.lat - alvo[1]) ** 2 <= raio_graus ** 2

for perfil, dia in [(Profile.LOCACAO, date(2026,8,4)),
                    (Profile.ENTREGA_POSTERIOR, date(2026,8,13))]:
    with connect(perfil) as c:
        paradas = build_source(perfil, c).fetch(dia, ImportMode.REPLANEJAR)
    conf, src, fora, ruins, indeterminados = Counter(), Counter(), [], [], []
    bons = 0
    for s in paradas:
        _, g = geo.geocode(s.address)
        conf[g.confidence] += 1; src[f'{g.confidence}/{g.source}'] += 1
        if g.confidence in ('high','medium'):
            ok = plausivel(g, s.address)
            if ok is True:                      # cidade desconhecida NAO conta
                bons += 1
            elif ok is None:
                indeterminados.append(s.address.raw)
            else:
                fora.append((s.address.raw, s.address.cidade, g.source,
                             round(g.lon,4), round(g.lat,4)))
        else:
            ruins.append((s.address.raw, s.address.bairro, g.confidence, g.source))
    n = max(len(paradas), 1)
    print(f'== {perfil.value}: {len(paradas)} paradas')
    print('   confianca:', dict(conf))
    print('   por fonte:', dict(src))
    print(f'   BOM (high+medium E perto da cidade certa) = {bons}/{len(paradas)} ({bons/n:.0%})')
    if fora:
        print(f'   !! {len(fora)} marcados bons mas LONGE da cidade informada:')
        for a in fora[:10]: print('      ', a)
    if indeterminados:
        print(f'   ?? {len(indeterminados)} com cidade fora do indice (nao contados):')
        for a in indeterminados[:5]: print('      ', a)
    if ruins:
        print(f'   -- {len(ruins)} em low/failed:')
        for a in ruins[:15]: print('      ', a)
"
```

Reportar os números como saíram, incluindo a lista de fora-da-caixa. Se ficar abaixo de 70%, **não relaxar o critério nem alargar a caixa** — listar os endereços que falharam e dizer o que neles derrotou a cascata. Esse diagnóstico decide se o próximo passo é `normalize.py`, o índice, ou a UI de correção manual; um número maquiado manda na direção errada e só aparece na frente do cliente.

- [ ] **Step 6: Commit**

```bash
git add api/app/geo/geocoder.py tests/test_geocoder.py
git commit -m "feat: geocoder em cascata com cache e pin manual"
```

---

## Task 7: StopSource — Entrega Posterior

**Files:**
- Create: `api/app/erp/base.py`, `api/app/erp/entrega_posterior.py`
- Test: `tests/test_erp_entrega.py`

**Interfaces:**
- Consumes: `ErpConnection` (Task 3), `Stop`/`Address`/`BaselineTrip` (Task 2), `ImportMode`/`Profile` (Task 2)
- Produces:
  - `class StopSource(Protocol)` com `profile: Profile`, `fetch(target_date: date, mode: ImportMode) -> list[Stop]`, `baseline_order(stops: list[Stop]) -> list[BaselineTrip]`
  - `EntregaPosteriorSource(conn: ErpConnection)` implementando o protocolo
  - `build_source(profile: Profile, conn: ErpConnection) -> StopSource` (registrado na Task 8)

- [ ] **Step 1: Escrever `api/app/erp/base.py`**

```python
from __future__ import annotations
from datetime import date
from typing import Protocol

from ..config import ImportMode, Profile
from ..models import BaselineTrip, Stop


class StopSource(Protocol):
    profile: Profile

    def fetch(self, target_date: date, mode: ImportMode) -> list[Stop]: ...

    def baseline_order(self, stops: list[Stop]) -> list[BaselineTrip]: ...
```

- [ ] **Step 2: Escrever o teste**

`2024-12-09` é o dia de pico verificado (215 paradas). `2026-08-13` é o último dia com movimento (18 paradas, 7 veículos).

```python
# tests/test_erp_entrega.py
from datetime import date

import pytest

from api.app.config import ImportMode, Profile
from api.app.db.firebird import connect
from api.app.erp.entrega_posterior import EntregaPosteriorSource

PICO = date(2024, 12, 9)
ULTIMO = date(2026, 8, 13)


@pytest.fixture(scope="module")
def src():
    with connect(Profile.ENTREGA_POSTERIOR) as c:
        yield EntregaPosteriorSource(c)


@pytest.mark.erp
def test_perfil(src):
    assert src.profile is Profile.ENTREGA_POSTERIOR


@pytest.mark.erp
def test_replanejar_traz_as_paradas_do_ultimo_dia(src):
    stops = src.fetch(ULTIMO, ImportMode.REPLANEJAR)
    assert 10 <= len(stops) <= 20
    assert all(s.external_id.startswith("EP:") for s in stops)


@pytest.mark.erp
def test_replanejar_traz_o_dia_de_pico(src):
    stops = src.fetch(PICO, ImportMode.REPLANEJAR)
    assert len(stops) > 190


@pytest.mark.erp
def test_producao_nao_traz_nada_em_base_historica(src):
    # SITUACAO = 1 tem zero linhas nesta base (ver §1.2.1 do spec)
    assert src.fetch(ULTIMO, ImportMode.PRODUCAO) == []


@pytest.mark.erp
def test_exclui_tipo_entrega_2_cliente_retira(src):
    with connect(Profile.ENTREGA_POSTERIOR) as c:
        rows = c.query(
            "SELECT FIRST 1 p.DATA FROM ENTREGA_PCAB p"
            " JOIN ENTREGA_CAB e ON e.ID_CONTROLE = p.ID_ENTREGA"
            " WHERE e.TIPO_ENTREGA = 2 AND p.SITUACAO <> 3")
    if not rows:
        pytest.skip("base sem TIPO_ENTREGA=2 fora de cancelados")
    dia = rows[0]["DATA"]
    ids = {s.external_id for s in src.fetch(dia, ImportMode.REPLANEJAR)}
    with connect(Profile.ENTREGA_POSTERIOR) as c:
        excl = c.query(
            "SELECT p.ID_CONTROLE FROM ENTREGA_PCAB p"
            " JOIN ENTREGA_CAB e ON e.ID_CONTROLE = p.ID_ENTREGA"
            " WHERE e.TIPO_ENTREGA = 2 AND p.DATA = ?", (dia,))
    for r in excl:
        assert f"EP:{int(r['ID_CONTROLE'])}" not in ids


@pytest.mark.erp
def test_nunca_traz_cancelados(src):
    stops = src.fetch(PICO, ImportMode.REPLANEJAR)
    with connect(Profile.ENTREGA_POSTERIOR) as c:
        canc = {f"EP:{int(r['ID_CONTROLE'])}" for r in c.query(
            "SELECT ID_CONTROLE FROM ENTREGA_PCAB WHERE DATA = ? AND SITUACAO = 3",
            (PICO,))}
    assert not ({s.external_id for s in stops} & canc)


@pytest.mark.erp
def test_endereco_preenchido_e_cidade_default(src):
    stops = src.fetch(ULTIMO, ImportMode.REPLANEJAR)
    com_rua = [s for s in stops if s.address.logradouro]
    assert len(com_rua) >= len(stops) * 0.8
    assert all(s.address.cidade for s in stops)


@pytest.mark.erp
def test_devolucao_vira_pickup(src):
    stops = src.fetch(PICO, ImportMode.REPLANEJAR)
    assert {s.kind for s in stops} <= {"delivery", "pickup"}


@pytest.mark.erp
def test_baseline_agrupa_por_veiculo_do_erp(src):
    stops = src.fetch(ULTIMO, ImportMode.REPLANEJAR)
    trips = src.baseline_order(stops)
    assert len(trips) >= 4
    total = sum(len(t.stop_external_ids) for t in trips)
    assert total == len(stops)


@pytest.mark.erp
def test_baseline_respeita_a_hora_registrada(src):
    stops = src.fetch(ULTIMO, ImportMode.REPLANEJAR)
    por_id = {s.external_id: s for s in stops}
    for t in src.baseline_order(stops):
        seqs = [por_id[i].erp_sequence for i in t.stop_external_ids]
        assert seqs == sorted(seqs)
```

- [ ] **Step 3: Rodar — deve falhar**

Run: `docker compose run --rm api pytest tests/test_erp_entrega.py -v`
Expected: FAIL com `ModuleNotFoundError: No module named 'api.app.erp.entrega_posterior'`

- [ ] **Step 4: Implementar `api/app/erp/entrega_posterior.py`**

Cuidado com tipos: nesta base `CLIFOR.ID`, `ENTREGA_CAB.ID_CLIENTE` e `ENTREGA_PCAB.ID_CONTROLE` são `FLOAT`, não inteiros. Converter com `int()` em toda fronteira, senão o `external_id` sai como `EP:162886.0`.

```python
from __future__ import annotations
from datetime import date

from ..config import ImportMode, Profile
from ..db.firebird import ErpConnection
from ..models import Address, BaselineTrip, Stop

_SQL = """
SELECT p.ID_CONTROLE AS PCAB_ID, p.ID_ENTREGA, p.DATA, p.HORA, p.ID_VEICULO,
       p.FLAG_DEV_VENDAS,
       e.ID_CLIENTE, e.CLIENTE_NOME, e.LOGRADOURO, e.CLIENTE_ENDERECO,
       e.CLIENTE_BAIRRO, e.CLIENTE_CIDADE, e.CLIENTE_UF, e.VENCIMENTO,
       e.DOCUMENTO, e.TIPO_ENTREGA,
       cf.NOME AS CF_NOME, cf.ENDERECO AS CF_ENDERECO, cf.NUMERO AS CF_NUMERO,
       cf.BAIRRO AS CF_BAIRRO, cf.CIDADE AS CF_CIDADE, cf.UF AS CF_UF, cf.CEP AS CF_CEP
FROM ENTREGA_PCAB p
JOIN ENTREGA_CAB e ON e.ID_CONTROLE = p.ID_ENTREGA
LEFT JOIN CLIFOR cf ON cf.ID = e.ID_CLIENTE
WHERE p.DATA = ?
  AND (e.TIPO_ENTREGA IS NULL OR e.TIPO_ENTREGA <> 2)
  AND {situacao}
ORDER BY p.HORA, p.ID_CONTROLE
"""

_SITUACAO = {
    ImportMode.PRODUCAO: "p.SITUACAO = 1",
    ImportMode.REPLANEJAR: "p.SITUACAO <> 3",
}


def _s(v) -> str | None:
    if v is None:
        return None
    t = str(v).strip()
    return t or None


class EntregaPosteriorSource:
    profile = Profile.ENTREGA_POSTERIOR

    def __init__(self, conn: ErpConnection):
        self._conn = conn

    def fetch(self, target_date: date, mode: ImportMode) -> list[Stop]:
        rows = self._conn.query(_SQL.format(situacao=_SITUACAO[mode]), (target_date,))
        stops: list[Stop] = []
        for seq, r in enumerate(rows):
            logradouro = _s(r["LOGRADOURO"]) or _s(r["CLIENTE_ENDERECO"]) \
                or _s(r["CF_ENDERECO"])
            addr = Address(
                logradouro=logradouro,
                numero=_s(r["CF_NUMERO"]),
                bairro=_s(r["CLIENTE_BAIRRO"]) or _s(r["CF_BAIRRO"]),
                cidade=_s(r["CLIENTE_CIDADE"]) or _s(r["CF_CIDADE"]),
                uf=_s(r["CLIENTE_UF"]) or _s(r["CF_UF"]),
                cep=_s(r["CF_CEP"]),
                raw=logradouro or "",
            )
            is_dev = int(r["FLAG_DEV_VENDAS"] or 0) == 1
            venc = r["VENCIMENTO"]
            atraso = (target_date - venc).days if venc and venc < target_date else 0
            stops.append(Stop(
                external_id=f"EP:{int(r['PCAB_ID'])}",
                kind="pickup" if is_dev else "delivery",
                cliente_id=int(r["ID_CLIENTE"] or 0),
                cliente_nome=_s(r["CLIENTE_NOME"]) or _s(r["CF_NOME"]) or "SEM NOME",
                address=addr,
                amount=1,
                priority=min(atraso * 5, 100),
                service_seconds=900 if is_dev else 600,
                due_date=venc,
                days_overdue=atraso,
                doc=_s(r["DOCUMENTO"]),
                notes=f"entrega {int(r['ID_ENTREGA'])}",
                erp_vehicle_id=int(r["ID_VEICULO"]) if r["ID_VEICULO"] else None,
                erp_sequence=seq,
            ))
        return stops

    def baseline_order(self, stops: list[Stop]) -> list[BaselineTrip]:
        """A rota que a operação realmente executou: agrupada por veículo do ERP,
        na ordem de HORA (já refletida em erp_sequence pelo ORDER BY do fetch)."""
        grupos: dict[int | None, list[Stop]] = {}
        for s in sorted(stops, key=lambda x: x.erp_sequence):
            grupos.setdefault(s.erp_vehicle_id, []).append(s)
        return [
            BaselineTrip(label=f"Veículo {vid}" if vid else "Sem veículo",
                         stop_external_ids=[s.external_id for s in grupo])
            for vid, grupo in sorted(grupos.items(), key=lambda kv: (kv[0] is None, kv[0]))
        ]
```

- [ ] **Step 5: Rodar os testes**

Run: `docker compose run --rm api pytest tests/test_erp_entrega.py -v`
Expected: 10 passed

- [ ] **Step 6: Commit**

```bash
git add api/app/erp/base.py api/app/erp/entrega_posterior.py tests/test_erp_entrega.py
git commit -m "feat: importacao de paradas do modulo de entrega posterior"
```

---

## Task 8: StopSource — Locação

**Files:**
- Create: `api/app/erp/locacao.py`
- Modify: `api/app/erp/base.py` (adicionar `build_source`)
- Test: `tests/test_erp_locacao.py`

**Interfaces:**
- Consumes: tudo da Task 7
- Produces: `LocacaoSource(conn: ErpConnection, overdue_days: int = 30)`; `build_source(profile: Profile, conn: ErpConnection) -> StopSource`

- [ ] **Step 1: Escrever o teste**

`2026-08-04` é a `MAX(DATA_LOCACAO)` verificada na base.

```python
# tests/test_erp_locacao.py
from datetime import date

import pytest

from api.app.config import ImportMode, Profile
from api.app.db.firebird import connect
from api.app.erp.base import build_source
from api.app.erp.locacao import LocacaoSource

DIA = date(2026, 8, 4)


@pytest.fixture(scope="module")
def src():
    with connect(Profile.LOCACAO) as c:
        yield LocacaoSource(c)


@pytest.mark.erp
def test_perfil(src):
    assert src.profile is Profile.LOCACAO


@pytest.mark.erp
def test_traz_entregas_do_dia(src):
    stops = src.fetch(DIA, ImportMode.REPLANEJAR)
    entregas = [s for s in stops if s.kind == "delivery"]
    assert len(entregas) >= 10
    assert all(s.external_id.startswith("LOC:") for s in entregas)


@pytest.mark.erp
def test_a_locacao_nao_tem_o_problema_da_base_historica(src):
    """Diferente da entrega posterior, aqui PRODUCAO devolve dados: as
    entregas vêm por DATA_LOCACAO, que não depende de nada estar pendente."""
    assert len(src.fetch(DIA, ImportMode.PRODUCAO)) > 0


@pytest.mark.erp
def test_replanejar_traz_as_devolucoes_reais_do_dia(src):
    """DATA_DEVOLUCAO é preenchida quando a coleta acontece, então num dia
    histórico ela É a carga de coleta real. Em 2026-08-04: 26 entregas, 12
    coletas — o caminhão sai cheio e volta cheio de verdade."""
    stops = src.fetch(DIA, ImportMode.REPLANEJAR)
    entregas = [s for s in stops if s.kind == "delivery"]
    coletas = [s for s in stops if s.kind == "pickup"]
    assert len(entregas) == 26
    assert len(coletas) == 12
    with connect(Profile.LOCACAO) as c:
        esperadas = {f"LOC:{int(r['ID_SEQUENCIA'])}" for r in c.query(
            "SELECT ID_SEQUENCIA FROM LOCACAO_PRODUTO WHERE DATA_DEVOLUCAO = ?",
            (DIA,))}
    assert {s.external_id for s in coletas} == esperadas


@pytest.mark.erp
def test_replanejar_nao_infla_prioridade_com_tempo_de_locacao(src):
    """Em dia histórico, dias na rua não é atraso — não deve virar prioridade."""
    coletas = [s for s in src.fetch(DIA, ImportMode.REPLANEJAR) if s.kind == "pickup"]
    assert all(s.days_overdue == 0 for s in coletas)
    assert all(0 <= s.priority <= 100 for s in coletas)


@pytest.mark.erp
def test_producao_traz_a_fila_de_vencidas_nao_as_devolvidas(src):
    """Em produção a coleta é o backlog: segue em locação e passou do corte.
    Em 2026-08-04 com 30 dias de corte são 4."""
    with connect(Profile.LOCACAO) as c:
        coletas = [s for s in LocacaoSource(c, overdue_days=30)
                   .fetch(DIA, ImportMode.PRODUCAO) if s.kind == "pickup"]
    assert len(coletas) == 4
    assert all(s.days_overdue > 30 for s in coletas)
    assert all(s.priority > 0 for s in coletas)


@pytest.mark.erp
def test_producao_nunca_coleta_algo_ja_devolvido(src):
    with connect(Profile.LOCACAO) as c:
        ids = {s.external_id for s in LocacaoSource(c, overdue_days=30)
               .fetch(DIA, ImportMode.PRODUCAO) if s.kind == "pickup"}
        devolvidas = {f"LOC:{int(r['ID_SEQUENCIA'])}" for r in c.query(
            "SELECT FIRST 500 ID_SEQUENCIA FROM LOCACAO_PRODUTO WHERE SITUACAO = 2")}
    assert not (ids & devolvidas)


@pytest.mark.erp
def test_usa_endereco_entrega_quando_existe(src):
    stops = src.fetch(DIA, ImportMode.REPLANEJAR)
    assert any(s.address.raw and s.address.raw.strip() for s in stops)


@pytest.mark.erp
def test_cai_no_cadastro_do_clifor_quando_endereco_entrega_esta_vazio(src):
    with connect(Profile.LOCACAO) as c:
        rows = c.query(
            "SELECT FIRST 1 DATA_LOCACAO FROM LOCACAO_PRODUTO"
            " WHERE (ENDERECO_ENTREGA IS NULL OR CHAR_LENGTH(TRIM(ENDERECO_ENTREGA)) <= 3)"
            " AND DATA_LOCACAO IS NOT NULL ORDER BY DATA_LOCACAO DESC")
    if not rows:
        pytest.skip("base sem endereço de entrega vazio")
    stops = src.fetch(rows[0]["DATA_LOCACAO"], ImportMode.REPLANEJAR)
    assert any(s.address.logradouro for s in stops)


@pytest.mark.erp
def test_teto_de_coletas_e_reportado_nao_engolido():
    """Com corte de 1 dia a fila de vencidas passa de 300 — é aí que o teto
    entra. Com 30 dias são só 4 e ele nunca dispara, então o teste tem de usar
    o corte curto, senão não testa nada."""
    with connect(Profile.LOCACAO) as c:
        apertado = LocacaoSource(c, overdue_days=1, max_pickups=5)
        stops = apertado.fetch(DIA, ImportMode.PRODUCAO)
        folgado = LocacaoSource(c, overdue_days=1, max_pickups=10_000)
        todas = folgado.fetch(DIA, ImportMode.PRODUCAO)
    n_apertado = len([s for s in stops if s.kind == "pickup"])
    n_todas = len([s for s in todas if s.kind == "pickup"])
    assert n_todas > 100, "corte de 1 dia deveria render uma fila grande"
    assert n_apertado == 5
    assert apertado.dropped_pickups == n_todas - 5
    assert folgado.dropped_pickups == 0


@pytest.mark.erp
def test_dropped_pickups_reseta_entre_fetches():
    """Teto 20: a fila de vencidas (300+) estoura, as 12 devoluções do dia não.
    Se `dropped_pickups` não fosse recalculado a cada fetch, o segundo assert
    veria o resto do primeiro."""
    with connect(Profile.LOCACAO) as c:
        src = LocacaoSource(c, overdue_days=1, max_pickups=20)
        src.fetch(DIA, ImportMode.PRODUCAO)
        assert src.dropped_pickups > 0
        src.fetch(DIA, ImportMode.REPLANEJAR)      # 12 coletas <= teto de 20
        assert src.dropped_pickups == 0


@pytest.mark.erp
def test_overdue_days_configuravel():
    with connect(Profile.LOCACAO) as c:
        curto = LocacaoSource(c, overdue_days=1, max_pickups=10_000) \
            .fetch(DIA, ImportMode.PRODUCAO)
        longo = LocacaoSource(c, overdue_days=365, max_pickups=10_000) \
            .fetch(DIA, ImportMode.PRODUCAO)
    n_curto = len([s for s in curto if s.kind == "pickup"])
    n_longo = len([s for s in longo if s.kind == "pickup"])
    assert n_curto > n_longo


@pytest.mark.erp
def test_baseline_particiona_pela_ordem_de_lancamento(src):
    stops = src.fetch(DIA, ImportMode.REPLANEJAR)
    trips = src.baseline_order(stops)
    assert len(trips) >= 1
    assert sum(len(t.stop_external_ids) for t in trips) == len(stops)
    por_id = {s.external_id: s for s in stops}
    primeira = [por_id[i].erp_sequence for i in trips[0].stop_external_ids]
    assert primeira == sorted(primeira)


@pytest.mark.erp
def test_build_source_resolve_os_dois_perfis():
    with connect(Profile.LOCACAO) as c:
        assert build_source(Profile.LOCACAO, c).profile is Profile.LOCACAO
    with connect(Profile.ENTREGA_POSTERIOR) as c:
        assert build_source(Profile.ENTREGA_POSTERIOR, c).profile \
            is Profile.ENTREGA_POSTERIOR
```

- [ ] **Step 2: Rodar — deve falhar**

Run: `docker compose run --rm api pytest tests/test_erp_locacao.py -v`
Expected: FAIL com `ModuleNotFoundError: No module named 'api.app.erp.locacao'`

- [ ] **Step 3: Implementar `api/app/erp/locacao.py`**

A locação não tem agenda no ERP, então as coletas são derivadas: tudo que continua `SITUACAO = 1` e foi locado há mais de `overdue_days` é candidato a recolher. O `baseline_order` particiona a ordem de lançamento em blocos — é o que o operador faz trabalhando de cima para baixo na lista impressa (§5.4 do spec).

```python
from __future__ import annotations
from datetime import date, timedelta

from ..config import ImportMode, Profile
from ..db.firebird import ErpConnection
from ..models import Address, BaselineTrip, Stop

_ENTREGAS = """
SELECT lp.ID_SEQUENCIA, lp.DATA_LOCACAO, lp.DOCUMENTO, lp.ID_CLIENTE,
       lp.ENDERECO_ENTREGA, lp.QUANTIDADE, lp.DATA_DEVOLUCAO,
       cf.NOME, cf.ENDERECO, cf.NUMERO, cf.BAIRRO, cf.CIDADE, cf.UF, cf.CEP,
       it.DESCRICAO AS PRODUTO
FROM LOCACAO_PRODUTO lp
LEFT JOIN CLIFOR cf ON cf.ID = lp.ID_CLIENTE
LEFT JOIN ITEM it ON it.ID_ITEM = lp.ID_PRODUTO
WHERE lp.DATA_LOCACAO = ?
ORDER BY lp.ID_SEQUENCIA
"""

# Coleta em REPLANEJAR: o que foi de fato devolvido naquele dia. `DATA_DEVOLUCAO`
# é preenchida retrospectivamente, quando a coleta acontece — então para
# replanejar um dia histórico ela É a carga de coleta real daquele dia.
_COLETAS_REPLANEJAR = """
SELECT lp.ID_SEQUENCIA, lp.DATA_LOCACAO, lp.DOCUMENTO, lp.ID_CLIENTE,
       lp.ENDERECO_ENTREGA, lp.QUANTIDADE, lp.DATA_DEVOLUCAO,
       cf.NOME, cf.ENDERECO, cf.NUMERO, cf.BAIRRO, cf.CIDADE, cf.UF, cf.CEP,
       it.DESCRICAO AS PRODUTO
FROM LOCACAO_PRODUTO lp
LEFT JOIN CLIFOR cf ON cf.ID = lp.ID_CLIENTE
LEFT JOIN ITEM it ON it.ID_ITEM = lp.ID_PRODUTO
WHERE lp.DATA_DEVOLUCAO = ?
ORDER BY lp.ID_SEQUENCIA
"""

# Coleta em PRODUCAO: a fila de vencidas — segue em locação e passou do corte.
_COLETAS_PRODUCAO = """
SELECT lp.ID_SEQUENCIA, lp.DATA_LOCACAO, lp.DOCUMENTO, lp.ID_CLIENTE,
       lp.ENDERECO_ENTREGA, lp.QUANTIDADE, lp.DATA_DEVOLUCAO,
       cf.NOME, cf.ENDERECO, cf.NUMERO, cf.BAIRRO, cf.CIDADE, cf.UF, cf.CEP,
       it.DESCRICAO AS PRODUTO
FROM LOCACAO_PRODUTO lp
LEFT JOIN CLIFOR cf ON cf.ID = lp.ID_CLIENTE
LEFT JOIN ITEM it ON it.ID_ITEM = lp.ID_PRODUTO
WHERE lp.SITUACAO = 1
  AND lp.DATA_DEVOLUCAO IS NULL
  AND lp.DATA_LOCACAO <= ?
ORDER BY lp.DATA_LOCACAO, lp.ID_SEQUENCIA
"""


def _s(v) -> str | None:
    if v is None:
        return None
    t = str(v).strip()
    return t or None


class LocacaoSource:
    profile = Profile.LOCACAO

    def __init__(self, conn: ErpConnection, overdue_days: int = 30,
                 max_pickups: int = 60):
        self._conn = conn
        self._overdue_days = overdue_days
        self._max_pickups = max_pickups
        self.dropped_pickups = 0        # coletas vencidas que não couberam no teto

    # ------------------------------------------------------------------
    def _address(self, r: dict) -> Address:
        entrega = _s(r["ENDERECO_ENTREGA"])
        cadastro = _s(r["ENDERECO"])
        # Textos muito curtos ("SN", "-") não servem; cai no cadastro.
        usar_entrega = entrega is not None and len(entrega) > 3
        logradouro = entrega if usar_entrega else cadastro
        return Address(
            logradouro=logradouro,
            numero=None if usar_entrega else _s(r["NUMERO"]),
            bairro=_s(r["BAIRRO"]),
            cidade=_s(r["CIDADE"]),
            uf=_s(r["UF"]),
            cep=_s(r["CEP"]),
            raw=logradouro or "",
        )

    def _stop(self, r: dict, kind: str, seq: int, target_date: date,
              mode: ImportMode) -> Stop:
        locado_em = r["DATA_LOCACAO"]
        # Em PRODUCAO isto é atraso de verdade. Em REPLANEJAR é só há quantos
        # dias o equipamento estava na rua — não deve inflar prioridade.
        dias_na_rua = (target_date - locado_em).days if locado_em else 0
        vencida = kind == "pickup" and mode is ImportMode.PRODUCAO
        return Stop(
            external_id=f"LOC:{int(r['ID_SEQUENCIA'])}",
            kind=kind,
            cliente_id=int(r["ID_CLIENTE"] or 0),
            cliente_nome=_s(r["NOME"]) or "SEM NOME",
            address=self._address(r),
            amount=max(int(r["QUANTIDADE"] or 1), 1),
            priority=(min(max(dias_na_rua - self._overdue_days, 0), 100) if vencida
                      else (20 if kind == "pickup" else 10)),
            service_seconds=1500 if kind == "pickup" else 900,
            due_date=None,
            days_overdue=max(dias_na_rua, 0) if vencida else 0,
            doc=_s(r["DOCUMENTO"]),
            notes=_s(r["PRODUTO"]) or "",
            erp_vehicle_id=None,
            erp_sequence=seq,
        )

    # ------------------------------------------------------------------
    def fetch(self, target_date: date, mode: ImportMode) -> list[Stop]:
        stops: list[Stop] = []
        seq = 0

        for r in self._conn.query(_ENTREGAS, (target_date,)):
            stops.append(self._stop(r, "delivery", seq, target_date, mode))
            seq += 1

        if mode is ImportMode.REPLANEJAR:
            # Dia histórico: a coleta real é o que foi devolvido nele.
            coletas = self._conn.query(_COLETAS_REPLANEJAR, (target_date,))
        else:
            corte = target_date - timedelta(days=self._overdue_days)
            coletas = self._conn.query(_COLETAS_PRODUCAO, (corte,))

        # Teto para uma fila de vencidas não afogar a rota do dia. O que sobra
        # NÃO some em silêncio: vai para dropped_pickups e a API devolve o
        # número, senão a tela mente dizendo que cobriu tudo.
        self.dropped_pickups = max(len(coletas) - self._max_pickups, 0)
        for r in coletas[: self._max_pickups]:
            stops.append(self._stop(r, "pickup", seq, target_date, mode))
            seq += 1

        return stops

    def baseline_order(self, stops: list[Stop]) -> list[BaselineTrip]:
        """Aproximado (§5.4 do spec): o ERP não registra veículo nem ordem para
        locação. Reproduz o operador trabalhando de cima para baixo na lista."""
        if not stops:
            return []
        ordenadas = sorted(stops, key=lambda s: s.erp_sequence)
        n_trips = max(1, round(len(ordenadas) / 12))
        tamanho = -(-len(ordenadas) // n_trips)          # ceil
        return [
            BaselineTrip(label=f"Viagem {i + 1}",
                         stop_external_ids=[s.external_id
                                            for s in ordenadas[i * tamanho:(i + 1) * tamanho]])
            for i in range(n_trips)
            if ordenadas[i * tamanho:(i + 1) * tamanho]
        ]
```

- [ ] **Step 4: Adicionar `build_source` ao final de `api/app/erp/base.py`**

```python
def build_source(profile: Profile, conn) -> StopSource:
    from .entrega_posterior import EntregaPosteriorSource
    from .locacao import LocacaoSource

    if profile is Profile.LOCACAO:
        return LocacaoSource(conn)
    if profile is Profile.ENTREGA_POSTERIOR:
        return EntregaPosteriorSource(conn)
    raise ValueError(f"perfil sem StopSource: {profile}")
```

- [ ] **Step 5: Rodar os testes**

Run: `docker compose run --rm api pytest tests/test_erp_locacao.py tests/test_erp_entrega.py -v`
Expected: 20 passed

- [ ] **Step 6: Commit**

```bash
git add api/app/erp/ tests/test_erp_locacao.py
git commit -m "feat: importacao de paradas de locacao com coletas de vencidas"
```

---

## Task 9: Cliente OSRM

**Files:**
- Create: `api/app/routing/osrm.py`
- Test: `tests/test_osrm.py`

**Interfaces:**
- Consumes: `Coord` de `api.app.models`
- Produces: `Matrix(durations: list[list[float]], distances: list[list[float]])`; `RouteGeometry(polyline: str, distance_m: int, duration_s: int)`; `OsrmClient(base_url: str, timeout: float = 60.0, max_snap_m: int = 5000)` com `table(coords) -> Matrix`, `route(coords) -> RouteGeometry`, `nearest(coord) -> Coord`; `OsrmError(RuntimeError)`

- [ ] **Step 1: Escrever o teste**

```python
# tests/test_osrm.py
import os
import pytest
from api.app.routing.osrm import OsrmClient, OsrmError

OSRM = os.environ.get("OSRM_URL", "http://osrm:5000")

CENTRO   = (-54.8060, -22.2210)
MARCELINO = (-54.8180, -22.2280)
JOAO_ROSA = (-54.7990, -22.2240)


@pytest.fixture(scope="module")
def client():
    return OsrmClient(OSRM)


@pytest.mark.stack
def test_table_dimensoes_e_diagonal_zero(client):
    m = client.table([CENTRO, MARCELINO, JOAO_ROSA])
    assert len(m.durations) == 3 and len(m.durations[0]) == 3
    assert len(m.distances) == 3
    assert m.durations[0][0] == 0
    assert m.distances[1][1] == 0


@pytest.mark.stack
def test_table_valores_plausiveis_dentro_de_dourados(client):
    m = client.table([CENTRO, MARCELINO])
    assert 100 < m.distances[0][1] < 20_000        # metros
    assert 10 < m.durations[0][1] < 3_600          # segundos


@pytest.mark.stack
def test_route_devolve_polyline_e_custos(client):
    r = client.route([CENTRO, MARCELINO, JOAO_ROSA])
    assert isinstance(r.polyline, str) and len(r.polyline) > 10
    assert r.distance_m > 0 and r.duration_s > 0


@pytest.mark.stack
def test_route_com_menos_de_dois_pontos_e_vazia(client):
    r = client.route([CENTRO])
    assert r.distance_m == 0 and r.duration_s == 0 and r.polyline == ""


@pytest.mark.stack
def test_nearest_gruda_na_via(client):
    lon, lat = client.nearest((-54.8061, -22.2211))
    assert abs(lon + 54.8) < 0.2 and abs(lat + 22.2) < 0.2


@pytest.mark.stack
def test_coordenada_fora_da_malha_levanta_erro(client):
    """Sem limite de snap o OSRM SEMPRE gruda no nó mais próximo do grafo, por
    mais absurda que seja a coordenada. Um lon/lat invertido viraria uma rota
    plausível e errada. O `radiuses` é o que transforma isso em erro alto."""
    with pytest.raises(OsrmError):
        client.route([(-30.0, -30.0), (-31.0, -31.0)])


@pytest.mark.stack
def test_lon_lat_invertido_e_recusado(client):
    """Dourados com lon/lat trocados cai no Atlântico Sul. É o erro mais fácil
    de cometer neste projeto e o mais caro — tem de estourar, não passar."""
    with pytest.raises(OsrmError):
        client.route([(-22.2210, -54.8060), (-22.2280, -54.8180)])


@pytest.mark.stack
def test_ponto_rural_distante_ainda_e_aceito(client):
    """O limite de snap não pode ser tão apertado que recuse entrega em
    chácara. ~12 km ao norte de Dourados, longe de via mapeada."""
    r = client.route([CENTRO, (-54.8060, -22.1100)])
    assert r.distance_m > 0


@pytest.mark.stack
def test_table_tambem_respeita_o_limite_de_snap(client):
    with pytest.raises(OsrmError):
        client.table([CENTRO, (-30.0, -30.0)])
```

- [ ] **Step 2: Rodar — deve falhar**

Run: `docker compose run --rm api pytest tests/test_osrm.py -v -m stack`
Expected: FAIL com `ModuleNotFoundError: No module named 'api.app.routing.osrm'`

- [ ] **Step 3: Implementar `api/app/routing/osrm.py`**

```python
from __future__ import annotations
from dataclasses import dataclass

import httpx

from ..models import Coord


class OsrmError(RuntimeError):
    pass


@dataclass
class Matrix:
    durations: list[list[float]]     # segundos
    distances: list[list[float]]     # metros


@dataclass
class RouteGeometry:
    polyline: str                    # polyline5
    distance_m: int
    duration_s: int


def _join(coords: list[Coord]) -> str:
    return ";".join(f"{lon:.6f},{lat:.6f}" for lon, lat in coords)


class OsrmClient:
    def __init__(self, base_url: str, timeout: float = 60.0,
                 max_snap_m: int = 5000):
        self._base = base_url.rstrip("/")
        self._http = httpx.Client(timeout=timeout)
        # Sem isto o OSRM gruda QUALQUER coordenada no nó mais próximo do
        # grafo, sem reclamar. Um lon/lat invertido ou um geocode lixo viraria
        # uma rota plausível e completamente errada. 5 km tolera entrega rural
        # longe de via mapeada e ainda assim recusa o que está noutro estado.
        self._max_snap_m = max_snap_m

    def _radiuses(self, n: int) -> str:
        return ";".join([str(self._max_snap_m)] * n)

    def _get(self, path: str, params: dict) -> dict:
        r = self._http.get(f"{self._base}{path}", params=params)
        if r.status_code >= 400:
            raise OsrmError(f"OSRM {r.status_code}: {r.text[:200]}")
        body = r.json()
        if body.get("code") != "Ok":
            raise OsrmError(f"OSRM {body.get('code')}: {body.get('message')}")
        return body

    def table(self, coords: list[Coord]) -> Matrix:
        body = self._get(f"/table/v1/driving/{_join(coords)}",
                         {"annotations": "duration,distance",
                          "radiuses": self._radiuses(len(coords))})
        return Matrix(durations=body["durations"], distances=body["distances"])

    def route(self, coords: list[Coord]) -> RouteGeometry:
        if len(coords) < 2:
            return RouteGeometry("", 0, 0)
        body = self._get(f"/route/v1/driving/{_join(coords)}",
                         {"overview": "full", "geometries": "polyline",
                          "radiuses": self._radiuses(len(coords))})
        route = body["routes"][0]
        return RouteGeometry(route["geometry"], int(route["distance"]),
                             int(route["duration"]))

    def nearest(self, coord: Coord) -> Coord:
        body = self._get(f"/nearest/v1/driving/{coord[0]:.6f},{coord[1]:.6f}",
                         {"number": 1})
        lon, lat = body["waypoints"][0]["location"]
        return (lon, lat)

    def close(self) -> None:
        self._http.close()
```

- [ ] **Step 4: Rodar os testes**

Run: `docker compose run --rm api pytest tests/test_osrm.py -v -m stack`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add api/app/routing/osrm.py tests/test_osrm.py
git commit -m "feat: cliente osrm para matriz, rota e snap"
```

---

## Task 10: Payload e leitura de solução do VROOM

**Files:**
- Create: `api/app/routing/vroom.py`
- Test: `tests/test_vroom.py`

**Interfaces:**
- Consumes: `Stop`, `VehicleConfig`, `Vehicle`, `Depot`, `Solution`, `VehicleRoute`, `RouteStep`, `Unassigned` de `api.app.models`
- Produces: `expand_trips(fleet: list[VehicleConfig], depot: Depot) -> list[Vehicle]`; `build_payload(stops: list[Stop], vehicles: list[Vehicle]) -> tuple[dict, dict[int, str], dict[int, Vehicle]]`; `parse_solution(body: dict, job_ids: dict[int, str], veh_ids: dict[int, Vehicle], stops: list[Stop]) -> Solution`; `NoGeocodedStops(RuntimeError)`

- [ ] **Step 1: Escrever o teste**

```python
# tests/test_vroom.py
import pytest

from api.app.models import (Address, Depot, GeoResult, Stop, VehicleConfig)
from api.app.routing.vroom import (NoGeocodedStops, build_payload,
                                   expand_trips, parse_solution)

DEPOT = Depot("Matriz", -54.8060, -22.2210)


def _stop(sid, kind="delivery", lon=-54.80, lat=-22.22, amount=1,
          priority=0, geocoded=True):
    a = Address("RUA X", "1", None, "DOURADOS", "MS", None, "RUA X, 1")
    s = Stop(external_id=sid, kind=kind, cliente_id=1, cliente_nome="C",
             address=a, amount=amount, priority=priority, service_seconds=600)
    if geocoded:
        s.geo = GeoResult(lon, lat, "high", "street_exact")
    return s


def _cfg(vid="A", trips=1, capacity=5):
    return VehicleConfig(id=vid, label=vid, capacity=capacity, trips=trips,
                         shift_start_s=7 * 3600, shift_end_s=19 * 3600)


# ---- expansão de viagens (§5.3 do spec) ---------------------------------
def test_expand_trips_um_caminhao_uma_viagem():
    vs = expand_trips([_cfg(trips=1)], DEPOT)
    assert len(vs) == 1
    assert vs[0].id == "A#1"
    assert vs[0].start == DEPOT.coord and vs[0].end == DEPOT.coord


def test_expand_trips_divide_a_jornada_entre_as_viagens():
    vs = expand_trips([_cfg(trips=3)], DEPOT)
    assert [v.id for v in vs] == ["A#1", "A#2", "A#3"]
    assert vs[0].shift_start_s == 7 * 3600
    assert vs[-1].shift_end_s == 19 * 3600
    assert vs[0].shift_end_s == vs[1].shift_start_s
    assert all(v.config_id == "A" for v in vs)


def test_expand_trips_ignora_veiculo_desabilitado():
    c = _cfg("B"); c.enabled = False
    assert expand_trips([_cfg("A"), c], DEPOT) == expand_trips([_cfg("A")], DEPOT)


# ---- payload -------------------------------------------------------------
def test_payload_entrega_usa_delivery_e_coleta_usa_pickup():
    stops = [_stop("s1", "delivery", amount=2), _stop("s2", "pickup", amount=3)]
    payload, jobs, _ = build_payload(stops, expand_trips([_cfg()], DEPOT))
    por_ext = {jobs[j["id"]]: j for j in payload["jobs"]}
    assert por_ext["s1"]["delivery"] == [2]
    assert "pickup" not in por_ext["s1"]
    assert por_ext["s2"]["pickup"] == [3]
    assert "delivery" not in por_ext["s2"]


def test_payload_ids_sao_inteiros_e_mapeaveis():
    stops = [_stop("EP:162886"), _stop("LOC:153543")]
    payload, jobs, vehs = build_payload(stops, expand_trips([_cfg()], DEPOT))
    assert all(isinstance(j["id"], int) for j in payload["jobs"])
    assert set(jobs.values()) == {"EP:162886", "LOC:153543"}
    assert all(isinstance(v["id"], int) for v in payload["vehicles"])
    assert set(vehs) == {v["id"] for v in payload["vehicles"]}


def test_payload_leva_capacidade_janela_prioridade_e_servico():
    stops = [_stop("s1", priority=77)]
    payload, _, _ = build_payload(stops, expand_trips([_cfg(capacity=9)], DEPOT))
    v = payload["vehicles"][0]
    assert v["capacity"] == [9]
    assert v["time_window"] == [7 * 3600, 19 * 3600]
    j = payload["jobs"][0]
    assert j["priority"] == 77
    assert j["service"] == 600
    assert j["location"] == [-54.80, -22.22]


def test_payload_descarta_parada_sem_geocodificacao():
    stops = [_stop("bom"), _stop("ruim", geocoded=False)]
    payload, jobs, _ = build_payload(stops, expand_trips([_cfg()], DEPOT))
    assert len(payload["jobs"]) == 1
    assert set(jobs.values()) == {"bom"}


def test_payload_sem_nenhuma_parada_geocodificada_levanta_erro():
    with pytest.raises(NoGeocodedStops):
        build_payload([_stop("x", geocoded=False)], expand_trips([_cfg()], DEPOT))


# ---- leitura da solução ---------------------------------------------------
def _body(job_id, veh_id):
    return {
        "code": 0,
        "routes": [{
            "vehicle": veh_id, "distance": 5400, "duration": 900,
            "steps": [
                {"type": "start", "location": [-54.806, -22.221], "arrival": 25200, "load": [1]},
                {"type": "job", "id": job_id, "location": [-54.80, -22.22],
                 "arrival": 25800, "load": [0]},
                {"type": "end", "location": [-54.806, -22.221], "arrival": 26400, "load": [0]},
            ],
        }],
        "unassigned": [],
        "summary": {"distance": 5400, "duration": 900},
    }


def test_parse_solution_monta_rota_com_sequencia():
    stops = [_stop("s1")]
    payload, jobs, vehs = build_payload(stops, expand_trips([_cfg()], DEPOT))
    jid = payload["jobs"][0]["id"]
    vid = payload["vehicles"][0]["id"]
    sol = parse_solution(_body(jid, vid), jobs, vehs, stops)
    assert len(sol.routes) == 1
    r = sol.routes[0]
    assert r.vehicle_id == "A#1" and r.config_id == "A" and r.trip_index == 1
    assert [s.stop_external_id for s in r.steps] == ["s1"]
    assert r.steps[0].seq == 1
    assert r.steps[0].arrival_s == 25800
    assert r.steps[0].kind == "delivery"
    assert r.distance_m == 5400 and r.duration_s == 900
    assert sol.total_distance_m == 5400 and sol.total_duration_s == 900


def test_parse_solution_registra_nao_atendidas():
    stops = [_stop("s1"), _stop("s2")]
    payload, jobs, vehs = build_payload(stops, expand_trips([_cfg()], DEPOT))
    jid = payload["jobs"][0]["id"]
    outro = payload["jobs"][1]["id"]
    body = _body(jid, payload["vehicles"][0]["id"])
    body["unassigned"] = [{"id": outro, "location": [-54.80, -22.22]}]
    sol = parse_solution(body, jobs, vehs, stops)
    assert [u.stop_external_id for u in sol.unassigned] == [jobs[outro]]


def test_parse_solution_marca_paradas_sem_geocodificacao_como_nao_atendidas():
    stops = [_stop("bom"), _stop("ruim", geocoded=False)]
    payload, jobs, vehs = build_payload(stops, expand_trips([_cfg()], DEPOT))
    body = _body(payload["jobs"][0]["id"], payload["vehicles"][0]["id"])
    sol = parse_solution(body, jobs, vehs, stops)
    assert any(u.stop_external_id == "ruim" and "geocodifica" in u.reason.lower()
               for u in sol.unassigned)
```

- [ ] **Step 2: Rodar — deve falhar**

Run: `docker compose run --rm api pytest tests/test_vroom.py -v`
Expected: FAIL com `ModuleNotFoundError: No module named 'api.app.routing.vroom'`

- [ ] **Step 3: Implementar `api/app/routing/vroom.py`**

O VROOM só aceita `id` inteiro em jobs e veículos. Os mapas `job_ids` e `veh_ids` são a ponte de volta para os identificadores do domínio, e por isso `build_payload` devolve os três juntos.

```python
from __future__ import annotations

from ..models import (Depot, Solution, Stop, Unassigned, Vehicle,
                      VehicleConfig, VehicleRoute, RouteStep)


class NoGeocodedStops(RuntimeError):
    """Nenhuma parada tem coordenada — não há o que otimizar."""


def expand_trips(fleet: list[VehicleConfig], depot: Depot) -> list[Vehicle]:
    """Cada caminhão físico vira N veículos VROOM, um por viagem, com janelas
    de turno sequenciais. VROOM não modela múltiplas viagens numa rota (§5.3)."""
    out: list[Vehicle] = []
    for cfg in fleet:
        if not cfg.enabled:
            continue
        n = max(1, cfg.trips)
        total = cfg.shift_end_s - cfg.shift_start_s
        passo = total // n
        for i in range(n):
            inicio = cfg.shift_start_s + i * passo
            fim = cfg.shift_end_s if i == n - 1 else inicio + passo
            out.append(Vehicle(
                id=f"{cfg.id}#{i + 1}", config_id=cfg.id,
                label=cfg.label if n == 1 else f"{cfg.label} — viagem {i + 1}",
                trip_index=i + 1, capacity=cfg.capacity,
                shift_start_s=inicio, shift_end_s=fim,
                start=depot.coord, end=depot.coord,
            ))
    return out


def build_payload(stops: list[Stop], vehicles: list[Vehicle]
                  ) -> tuple[dict, dict[int, str], dict[int, Vehicle]]:
    jobs: list[dict] = []
    job_ids: dict[int, str] = {}
    for i, s in enumerate(stops, start=1):
        if s.geo is None or s.geo.confidence == "failed":
            continue
        job: dict = {
            "id": i,
            "location": [s.geo.lon, s.geo.lat],
            "service": s.service_seconds,
            "priority": max(0, min(int(s.priority), 100)),
        }
        if s.kind == "pickup":
            job["pickup"] = [max(1, s.amount)]
        else:
            job["delivery"] = [max(1, s.amount)]
        jobs.append(job)
        job_ids[i] = s.external_id

    if not jobs:
        raise NoGeocodedStops("nenhuma parada geocodificada para otimizar")

    veh_ids: dict[int, Vehicle] = {}
    veh_payload: list[dict] = []
    for i, v in enumerate(vehicles, start=1):
        veh_ids[i] = v
        veh_payload.append({
            "id": i,
            "start": list(v.start),
            "end": list(v.end),
            "capacity": [v.capacity],
            "time_window": [v.shift_start_s, v.shift_end_s],
        })

    return {"jobs": jobs, "vehicles": veh_payload}, job_ids, veh_ids


def parse_solution(body: dict, job_ids: dict[int, str],
                   veh_ids: dict[int, Vehicle], stops: list[Stop]) -> Solution:
    por_ext = {s.external_id: s for s in stops}
    sol = Solution()

    for r in body.get("routes", []):
        v = veh_ids[r["vehicle"]]
        route = VehicleRoute(vehicle_id=v.id, config_id=v.config_id, label=v.label,
                             trip_index=v.trip_index,
                             distance_m=int(r.get("distance", 0)),
                             duration_s=int(r.get("duration", 0)))
        seq = 0
        for step in r.get("steps", []):
            if step.get("type") != "job":
                continue
            seq += 1
            ext = job_ids[step["id"]]
            lon, lat = step["location"]
            load = step.get("load") or [0]
            route.steps.append(RouteStep(
                seq=seq, stop_external_id=ext,
                kind=por_ext[ext].kind if ext in por_ext else "delivery",
                lon=lon, lat=lat,
                arrival_s=int(step.get("arrival", 0)), load_after=int(load[0]),
            ))
        sol.routes.append(route)

    for u in body.get("unassigned", []):
        ext = job_ids.get(u.get("id"))
        if ext:
            sol.unassigned.append(Unassigned(ext, "não coube na frota/janela do dia"))

    atendidas = {s.stop_external_id for r in sol.routes for s in r.steps}
    atendidas |= {u.stop_external_id for u in sol.unassigned}
    for s in stops:
        if s.external_id not in atendidas:
            sol.unassigned.append(
                Unassigned(s.external_id, "sem geocodificação — revisar no mapa"))

    summary = body.get("summary") or {}
    sol.total_distance_m = int(summary.get("distance",
                                           sum(r.distance_m for r in sol.routes)))
    sol.total_duration_s = int(summary.get("duration",
                                           sum(r.duration_s for r in sol.routes)))
    return sol
```

- [ ] **Step 4: Rodar os testes**

Run: `docker compose run --rm api pytest tests/test_vroom.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add api/app/routing/vroom.py tests/test_vroom.py
git commit -m "feat: modelagem vroom com viagens multiplas e carga mista entrega/coleta"
```

---

## Task 11: Optimizer, baseline e comparativo

**Files:**
- Create: `api/app/routing/optimizer.py`, `api/app/routing/baseline.py`
- Test: `tests/test_optimizer.py`, `tests/test_baseline.py`

**Interfaces:**
- Consumes: `OsrmClient` (Task 9), `expand_trips`/`build_payload`/`parse_solution`/`NoGeocodedStops` (Task 10), modelos (Task 2)
- Produces:
  - `Optimizer(vroom_url: str, osrm: OsrmClient, timeout_s: int = 20)` com `solve(stops, fleet, depot) -> Solution` (preenche `VehicleRoute.geometry` via OSRM)
  - `VroomError(RuntimeError)`
  - `measure_baseline(trips, stops, osrm, depot) -> BaselineResult`
  - `compare(solution, baseline, cost_per_km: float = 3.50, workdays: int = 22) -> Comparison`

- [ ] **Step 1: Escrever `tests/test_baseline.py`**

```python
import pytest

from api.app.models import (Address, BaselineTrip, Depot, GeoResult, RouteStep,
                            Solution, Stop, VehicleRoute)
from api.app.routing.baseline import compare, measure_baseline

DEPOT = Depot("Matriz", -54.8060, -22.2210)


class FakeOsrm:
    """Cobra 1000 m e 120 s por perna, para tornar o cálculo verificável à mão."""
    def __init__(self):
        self.chamadas = []

    def route(self, coords):
        from api.app.routing.osrm import RouteGeometry
        self.chamadas.append(list(coords))
        pernas = max(len(coords) - 1, 0)
        return RouteGeometry("xx", 1000 * pernas, 120 * pernas)


def _stop(sid, service=600):
    a = Address("RUA X", "1", None, "DOURADOS", "MS", None, "RUA X, 1")
    s = Stop(external_id=sid, kind="delivery", cliente_id=1, cliente_nome="C",
             address=a, service_seconds=service)
    s.geo = GeoResult(-54.80, -22.22, "high", "street_exact")
    return s


def test_measure_fecha_o_circuito_no_deposito():
    osrm = FakeOsrm()
    stops = [_stop("a"), _stop("b")]
    trips = [BaselineTrip("Veículo 1", ["a", "b"])]
    measure_baseline(trips, stops, osrm, DEPOT)
    assert osrm.chamadas[0][0] == DEPOT.coord
    assert osrm.chamadas[0][-1] == DEPOT.coord
    assert len(osrm.chamadas[0]) == 4          # depósito + 2 paradas + depósito


def test_measure_soma_distancia_e_inclui_tempo_de_servico():
    stops = [_stop("a", 600), _stop("b", 600)]
    trips = [BaselineTrip("Veículo 1", ["a", "b"])]
    r = measure_baseline(trips, stops, FakeOsrm(), DEPOT)
    assert r.total_distance_m == 3000           # 3 pernas
    assert r.total_duration_s == 3 * 120 + 1200 # deslocamento + serviço
    assert r.vehicles_used == 1


def test_measure_ignora_parada_sem_geocodificacao():
    a, b = _stop("a"), _stop("b")
    b.geo = None
    r = measure_baseline([BaselineTrip("V1", ["a", "b"])], [a, b], FakeOsrm(), DEPOT)
    assert r.total_distance_m == 2000           # só depósito -> a -> depósito


def test_measure_sem_paradas_nao_conta_veiculo():
    r = measure_baseline([BaselineTrip("V1", [])], [], FakeOsrm(), DEPOT)
    assert r.vehicles_used == 0 and r.total_distance_m == 0


def test_measure_marca_aproximado_quando_pedido():
    r = measure_baseline([], [], FakeOsrm(), DEPOT, approximate=True,
                         note="ordem de lançamento")
    assert r.approximate is True
    assert "lançamento" in r.note


def test_compare_calcula_economia():
    sol = Solution(routes=[VehicleRoute("A#1", "A", "A", 1, [], 8000, 3600)],
                   total_distance_m=8000, total_duration_s=3600)
    base = measure_baseline([BaselineTrip("V1", ["a", "b"])],
                            [_stop("a"), _stop("b")], FakeOsrm(), DEPOT)
    c = compare(sol, base, cost_per_km=4.00, workdays=20)
    assert c.baseline_km == pytest.approx(3.0)
    assert c.optimized_km == pytest.approx(8.0)
    assert c.km_saved == pytest.approx(-5.0)          # otimizado pior: reporta negativo
    assert c.monthly_brl_saved == pytest.approx(-5.0 * 4.00 * 20)


def test_compare_percentual_com_baseline_zero_nao_divide_por_zero():
    sol = Solution(total_distance_m=0, total_duration_s=0)
    base = measure_baseline([], [], FakeOsrm(), DEPOT)
    c = compare(sol, base)
    assert c.percent_km_saved == 0.0


def test_compare_propaga_a_ressalva_do_baseline():
    sol = Solution(total_distance_m=1000, total_duration_s=60)
    base = measure_baseline([], [], FakeOsrm(), DEPOT, approximate=True, note="aprox")
    c = compare(sol, base)
    assert c.approximate is True and c.note == "aprox"
```

- [ ] **Step 2: Rodar — deve falhar**

Run: `docker compose run --rm api pytest tests/test_baseline.py -v`
Expected: FAIL com `ModuleNotFoundError: No module named 'api.app.routing.baseline'`

- [ ] **Step 3: Implementar `api/app/routing/baseline.py`**

O baseline mede a sequência que o ERP registrou, com o mesmo motor e o mesmo tempo de serviço da rota otimizada. Só a ordem e a alocação mudam entre os dois — é isso que torna o número defensável.

```python
from __future__ import annotations

from ..models import (BaselineResult, BaselineTrip, Comparison, Depot,
                      Solution, Stop)


def measure_baseline(trips: list[BaselineTrip], stops: list[Stop], osrm,
                     depot: Depot, approximate: bool = False,
                     note: str = "") -> BaselineResult:
    por_id = {s.external_id: s for s in stops}
    dist = dur = 0
    usados = 0

    for trip in trips:
        seq = [por_id[i] for i in trip.stop_external_ids
               if i in por_id and por_id[i].geo is not None
               and por_id[i].geo.confidence != "failed"]
        if not seq:
            continue
        usados += 1
        coords = [depot.coord] + [s.geo.coord for s in seq] + [depot.coord]
        leg = osrm.route(coords)
        dist += leg.distance_m
        dur += leg.duration_s + sum(s.service_seconds for s in seq)

    return BaselineResult(trips=trips, total_distance_m=dist, total_duration_s=dur,
                          vehicles_used=usados, approximate=approximate, note=note)


def compare(solution: Solution, baseline: BaselineResult,
            cost_per_km: float = 3.50, workdays: int = 22) -> Comparison:
    base_km = baseline.total_distance_m / 1000
    otim_km = solution.total_distance_m / 1000
    base_h = baseline.total_duration_s / 3600
    otim_h = solution.total_duration_s / 3600
    km_saved = base_km - otim_km

    return Comparison(
        baseline_km=round(base_km, 1),
        optimized_km=round(otim_km, 1),
        baseline_hours=round(base_h, 1),
        optimized_hours=round(otim_h, 1),
        km_saved=round(km_saved, 1),
        hours_saved=round(base_h - otim_h, 1),
        percent_km_saved=round(km_saved / base_km * 100, 1) if base_km else 0.0,
        monthly_brl_saved=round(km_saved * cost_per_km * workdays, 2),
        approximate=baseline.approximate,
        note=baseline.note,
    )
```

- [ ] **Step 4: Rodar os testes do baseline**

Run: `docker compose run --rm api pytest tests/test_baseline.py -v`
Expected: 8 passed

- [ ] **Step 5: Escrever `tests/test_optimizer.py`**

```python
import os

import pytest

from api.app.models import Address, Depot, GeoResult, Stop, VehicleConfig
from api.app.routing.optimizer import Optimizer, VroomError
from api.app.routing.osrm import OsrmClient
from api.app.routing.vroom import NoGeocodedStops

OSRM = os.environ.get("OSRM_URL", "http://osrm:5000")
VROOM = os.environ.get("VROOM_URL", "http://vroom:3000")

DEPOT = Depot("Matriz", -54.8060, -22.2210)
PONTOS = [(-54.8180, -22.2280), (-54.7990, -22.2240), (-54.8100, -22.2350),
          (-54.7950, -22.2150), (-54.8250, -22.2100), (-54.8020, -22.2400)]


def _stop(i, lon, lat, kind="delivery"):
    a = Address("RUA X", str(i), None, "DOURADOS", "MS", None, "RUA X")
    s = Stop(external_id=f"S{i}", kind=kind, cliente_id=i, cliente_nome=f"C{i}",
             address=a, service_seconds=300)
    s.geo = GeoResult(lon, lat, "high", "street_exact")
    return s


@pytest.fixture(scope="module")
def opt():
    return Optimizer(VROOM, OsrmClient(OSRM))


@pytest.mark.stack
def test_atende_todas_as_paradas_com_frota_folgada(opt):
    stops = [_stop(i, lon, lat) for i, (lon, lat) in enumerate(PONTOS)]
    fleet = [VehicleConfig(id="A", label="Caminhão A", capacity=10, trips=1)]
    sol = opt.solve(stops, fleet, DEPOT)
    atendidas = {s.stop_external_id for r in sol.routes for s in r.steps}
    assert atendidas == {s.external_id for s in stops}
    assert sol.unassigned == []
    assert sol.total_distance_m > 0


@pytest.mark.stack
def test_preenche_geometria_de_cada_rota(opt):
    stops = [_stop(i, lon, lat) for i, (lon, lat) in enumerate(PONTOS[:3])]
    sol = opt.solve(stops, [VehicleConfig(id="A", label="A", capacity=10)], DEPOT)
    assert all(len(r.geometry) > 10 for r in sol.routes if r.steps)


@pytest.mark.stack
def test_capacidade_um_com_seis_viagens_atende_carga_mista(opt):
    """Caso poliguindaste: leva uma caçamba por vez, alterna entrega e coleta."""
    stops = [_stop(i, lon, lat, "delivery" if i % 2 == 0 else "pickup")
             for i, (lon, lat) in enumerate(PONTOS)]
    fleet = [VehicleConfig(id="MB", label="MB 1513", capacity=1, trips=6,
                           shift_start_s=7 * 3600, shift_end_s=19 * 3600)]
    sol = opt.solve(stops, fleet, DEPOT)
    atendidas = {s.stop_external_id for r in sol.routes for s in r.steps}
    assert len(atendidas) >= 4
    for r in sol.routes:
        assert all(step.load_after <= 1 for step in r.steps)


@pytest.mark.stack
def test_frota_insuficiente_devolve_nao_atendidas(opt):
    stops = [_stop(i, lon, lat) for i, (lon, lat) in enumerate(PONTOS)]
    fleet = [VehicleConfig(id="A", label="A", capacity=2, trips=1,
                           shift_start_s=7 * 3600, shift_end_s=8 * 3600)]
    sol = opt.solve(stops, fleet, DEPOT)
    assert len(sol.unassigned) > 0


@pytest.mark.stack
def test_prioridade_alta_e_atendida_antes(opt):
    stops = [_stop(i, lon, lat) for i, (lon, lat) in enumerate(PONTOS)]
    stops[-1].priority = 100
    fleet = [VehicleConfig(id="A", label="A", capacity=3, trips=1,
                           shift_start_s=7 * 3600, shift_end_s=9 * 3600)]
    sol = opt.solve(stops, fleet, DEPOT)
    atendidas = {s.stop_external_id for r in sol.routes for s in r.steps}
    assert stops[-1].external_id in atendidas


@pytest.mark.stack
def test_sem_paradas_geocodificadas_levanta_erro(opt):
    s = _stop(1, 0, 0); s.geo = None
    with pytest.raises(NoGeocodedStops):
        opt.solve([s], [VehicleConfig(id="A", label="A")], DEPOT)


@pytest.mark.stack
def test_frota_vazia_levanta_erro(opt):
    stops = [_stop(0, *PONTOS[0])]
    with pytest.raises(VroomError):
        opt.solve(stops, [], DEPOT)
```

- [ ] **Step 6: Rodar — deve falhar**

Run: `docker compose run --rm api pytest tests/test_optimizer.py -v -m stack`
Expected: FAIL com `ModuleNotFoundError: No module named 'api.app.routing.optimizer'`

- [ ] **Step 7: Implementar `api/app/routing/optimizer.py`**

A geometria vem de uma chamada `/route` ao OSRM sobre a sequência já resolvida, em vez de pedir ao VROOM. Uma chamada por rota, e não depende da versão do vroom-express suportar `options.g`.

```python
from __future__ import annotations

import httpx

from ..models import Depot, Solution, Stop, VehicleConfig
from .osrm import OsrmClient, OsrmError
from .vroom import build_payload, expand_trips, parse_solution


class VroomError(RuntimeError):
    pass


class Optimizer:
    def __init__(self, vroom_url: str, osrm: OsrmClient, timeout_s: int = 20):
        self._url = vroom_url.rstrip("/")
        self._osrm = osrm
        self._timeout = timeout_s

    def solve(self, stops: list[Stop], fleet: list[VehicleConfig],
              depot: Depot) -> Solution:
        vehicles = expand_trips(fleet, depot)
        if not vehicles:
            raise VroomError("nenhum veículo habilitado na frota")

        payload, job_ids, veh_ids = build_payload(stops, vehicles)

        try:
            r = httpx.post(self._url, json=payload, timeout=self._timeout + 10)
        except httpx.HTTPError as exc:
            raise VroomError(f"falha ao chamar o VROOM: {exc}") from exc
        if r.status_code >= 400:
            raise VroomError(f"VROOM {r.status_code}: {r.text[:300]}")

        body = r.json()
        if body.get("code") != 0:
            raise VroomError(f"VROOM code={body.get('code')}: {body.get('error')}")

        solution = parse_solution(body, job_ids, veh_ids, stops)
        self._fill_geometry(solution, depot)
        return solution

    def _fill_geometry(self, solution: Solution, depot: Depot) -> None:
        for route in solution.routes:
            if not route.steps:
                continue
            coords = [depot.coord] + [(s.lon, s.lat) for s in route.steps] \
                + [depot.coord]
            try:
                route.geometry = self._osrm.route(coords).polyline
            except (OsrmError, httpx.HTTPError):
                # Geometria é cosmética: sem ela o mapa desenha só os pinos e a
                # rota continua válida. Erros fora desses dois sobem — são bugs.
                route.geometry = ""
```

- [ ] **Step 8: Rodar os testes**

Run: `docker compose run --rm api pytest tests/test_optimizer.py -v -m stack`
Expected: 7 passed

- [ ] **Step 9: Commit**

```bash
git add api/app/routing/optimizer.py api/app/routing/baseline.py tests/test_optimizer.py tests/test_baseline.py
git commit -m "feat: orquestracao do solver, baseline do erp e comparativo em km/horas/reais"
```

---

## Task 12: Romaneio PDF, CSV e deep links de navegação

**Files:**
- Create: `api/app/export/maps_link.py`, `api/app/export/romaneio.py`
- Test: `tests/test_export.py`

**Interfaces:**
- Consumes: `VehicleRoute`, `RouteStep`, `Stop` (Task 2)
- Produces:
  - `google_maps_link(route: VehicleRoute, depot: Depot) -> str`
  - `waze_link(step: RouteStep) -> str`
  - `build_romaneio_pdf(route: VehicleRoute, stops: list[Stop], depot: Depot, profile_label: str, target_date: str) -> bytes`
  - `build_csv(routes: list[VehicleRoute], stops: list[Stop]) -> str`

- [ ] **Step 1: Escrever o teste**

```python
# tests/test_export.py
import csv
import io
from urllib.parse import parse_qs, urlparse

import pytest

from api.app.export.maps_link import google_maps_link, waze_link
from api.app.export.romaneio import build_csv, build_romaneio_pdf
from api.app.models import Address, Depot, RouteStep, Stop, VehicleRoute

DEPOT = Depot("Matriz", -54.8060, -22.2210)


def _step(seq, ext, lon, lat, kind="delivery", arrival=28800):
    return RouteStep(seq=seq, stop_external_id=ext, kind=kind, lon=lon, lat=lat,
                     arrival_s=arrival, load_after=0)


def _stop(ext, nome="CLIENTE X", rua="RUA MATO GROSSO, 1973", kind="delivery"):
    a = Address(rua, None, "CENTRO", "DOURADOS", "MS", None, rua)
    return Stop(external_id=ext, kind=kind, cliente_id=1, cliente_nome=nome,
                address=a, doc="12345", notes="CACAMBA 5M3")


@pytest.fixture
def rota():
    return VehicleRoute("MB#1", "MB", "MB 1513 — viagem 1", 1, [
        _step(1, "LOC:1", -54.8180, -22.2280),
        _step(2, "LOC:2", -54.7990, -22.2240, "pickup", 30600),
    ], distance_m=12400, duration_s=3600)


# ---- deep links ----------------------------------------------------------
def test_google_maps_link_abre_e_fecha_no_deposito(rota):
    url = google_maps_link(rota, DEPOT)
    q = parse_qs(urlparse(url).query)
    assert q["origin"][0] == "-22.221,-54.806"
    assert q["destination"][0] == "-22.221,-54.806"
    assert q["travelmode"][0] == "driving"


def test_google_maps_link_leva_as_paradas_como_waypoints_na_ordem(rota):
    q = parse_qs(urlparse(google_maps_link(rota, DEPOT)).query)
    assert q["waypoints"][0] == "-22.228,-54.818|-22.224,-54.799"


def test_google_maps_link_de_rota_vazia_nao_explode():
    url = google_maps_link(VehicleRoute("A#1", "A", "A", 1, []), DEPOT)
    assert url.startswith("https://")


def test_waze_link_usa_lat_lon_e_navega_direto(rota):
    url = waze_link(rota.steps[0])
    assert "ll=-22.228%2C-54.818" in url or "ll=-22.228,-54.818" in url
    assert "navigate=yes" in url


# ---- CSV -----------------------------------------------------------------
def test_csv_tem_cabecalho_e_uma_linha_por_parada(rota):
    stops = [_stop("LOC:1"), _stop("LOC:2", "CLIENTE Y", kind="pickup")]
    linhas = list(csv.DictReader(io.StringIO(build_csv([rota], stops)), delimiter=";"))
    assert len(linhas) == 2
    assert linhas[0]["veiculo"] == "MB 1513 — viagem 1"
    assert linhas[0]["seq"] == "1"
    assert linhas[0]["cliente"] == "CLIENTE X"
    assert linhas[0]["tipo"] == "Entrega"
    assert linhas[1]["tipo"] == "Coleta"
    assert linhas[0]["chegada"] == "08:00"


def test_csv_sem_rotas_devolve_so_cabecalho():
    texto = build_csv([], [])
    assert texto.strip().count("\n") == 0
    assert "veiculo" in texto


# ---- PDF -----------------------------------------------------------------
def test_pdf_e_um_pdf_valido(rota):
    stops = [_stop("LOC:1"), _stop("LOC:2", "CLIENTE Y", kind="pickup")]
    pdf = build_romaneio_pdf(rota, stops, DEPOT, "Locação", "2026-08-04")
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 1000


def test_pdf_de_rota_vazia_ainda_gera_documento():
    pdf = build_romaneio_pdf(VehicleRoute("A#1", "A", "A", 1, []), [], DEPOT,
                             "Locação", "2026-08-04")
    assert pdf[:5] == b"%PDF-"
```

- [ ] **Step 2: Rodar — deve falhar**

Run: `docker compose run --rm api pytest tests/test_export.py -v`
Expected: FAIL com `ModuleNotFoundError: No module named 'api.app.export.maps_link'`

- [ ] **Step 3: Implementar `api/app/export/maps_link.py`**

Google Maps e Waze usam `lat,lon` na URL — ordem inversa da interna. É o único outro lugar além do Leaflet que inverte.

```python
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
```

- [ ] **Step 4: Implementar `api/app/export/romaneio.py`**

`fonts-dejavu-core` foi instalado no `Dockerfile.api` da Task 1 exatamente para isto: as fontes base do reportlab não cobrem acentuação em todos os glifos usados nos nomes de cliente.

```python
from __future__ import annotations
import csv
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from ..models import Depot, Stop, VehicleRoute
from .maps_link import google_maps_link

_TIPO = {"delivery": "Entrega", "pickup": "Coleta"}
_FONTE = "DejaVuSans"

try:
    pdfmetrics.registerFont(
        TTFont(_FONTE, "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
except Exception:                                     # pragma: no cover
    _FONTE = "Helvetica"


def _hhmm(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}"


def build_csv(routes: list[VehicleRoute], stops: list[Stop]) -> str:
    por_id = {s.external_id: s for s in stops}
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow(["veiculo", "seq", "chegada", "tipo", "documento", "cliente",
                "endereco", "bairro", "cidade", "observacao", "lat", "lon"])
    for r in routes:
        for s in sorted(r.steps, key=lambda x: x.seq):
            st = por_id.get(s.stop_external_id)
            a = st.address if st else None
            w.writerow([
                r.label, s.seq, _hhmm(s.arrival_s), _TIPO.get(s.kind, s.kind),
                (st.doc if st else "") or "", st.cliente_nome if st else "",
                (a.logradouro if a else "") or "", (a.bairro if a else "") or "",
                (a.cidade if a else "") or "", (st.notes if st else "") or "",
                f"{s.lat:.6f}", f"{s.lon:.6f}",
            ])
    return buf.getvalue()


def build_romaneio_pdf(route: VehicleRoute, stops: list[Stop], depot: Depot,
                       profile_label: str, target_date: str) -> bytes:
    por_id = {s.external_id: s for s in stops}
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=15 * mm, bottomMargin=15 * mm,
                            leftMargin=12 * mm, rightMargin=12 * mm,
                            title=f"Romaneio {route.label}")
    styles = getSampleStyleSheet()
    for name in ("Normal", "Title", "Heading2"):
        styles[name].fontName = _FONTE
    celula = styles["Normal"].clone("celula")
    celula.fontSize = 8
    celula.leading = 10

    story = [
        Paragraph(f"Romaneio de rota — {route.label}", styles["Title"]),
        Paragraph(f"{profile_label} · {target_date} · saída de {depot.label} · "
                  f"{len(route.steps)} paradas · {route.distance_m / 1000:.1f} km · "
                  f"{route.duration_s // 3600}h{(route.duration_s % 3600) // 60:02d}",
                  styles["Normal"]),
        Spacer(1, 6 * mm),
    ]

    dados = [["#", "Hora", "Tipo", "Doc", "Cliente", "Endereço", "Obs."]]
    for s in sorted(route.steps, key=lambda x: x.seq):
        st = por_id.get(s.stop_external_id)
        a = st.address if st else None
        endereco = ", ".join(p for p in [
            (a.logradouro if a else None), (a.bairro if a else None),
            (a.cidade if a else None)] if p)
        dados.append([
            str(s.seq), _hhmm(s.arrival_s), _TIPO.get(s.kind, s.kind),
            Paragraph((st.doc if st else "") or "", celula),
            Paragraph(st.cliente_nome if st else s.stop_external_id, celula),
            Paragraph(endereco, celula),
            Paragraph((st.notes if st else "") or "", celula),
        ])

    tabela = Table(dados, repeatRows=1,
                   colWidths=[8 * mm, 14 * mm, 16 * mm, 18 * mm, 45 * mm, 60 * mm, 25 * mm])
    tabela.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), _FONTE),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3a5f")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b0b8c4")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f2f5f9")]),
    ]))
    story.append(tabela)
    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph(
        f'<link href="{google_maps_link(route, depot)}">Abrir rota no Google Maps</link>',
        styles["Normal"]))

    doc.build(story)
    return buf.getvalue()
```

- [ ] **Step 5: Rodar os testes**

Run: `docker compose run --rm api pytest tests/test_export.py -v`
Expected: 9 passed

- [ ] **Step 6: Commit**

```bash
git add api/app/export/ tests/test_export.py
git commit -m "feat: romaneio pdf, csv e deep links de google maps e waze"
```

---

## Task 13: API HTTP

**Files:**
- Create: `api/app/service.py`, `api/app/main.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: tudo das Tasks 2–12
- Produces:
  - `service.import_stops(profile, target_date, mode) -> tuple[list[Stop], dict]` — paradas já geocodificadas + contagens
  - `service.optimize(profile, target_date, mode, cost_per_km) -> dict` — payload completo persistido
  - `service.stop_payload(stop) -> dict`, `service.route_payload(route) -> dict`
  - App FastAPI em `api.app.main:app` com os endpoints abaixo

| Método | Rota | Corpo / query | Resposta |
|---|---|---|---|
| GET | `/api/profiles` | — | `[{profile, label}]` |
| POST | `/api/stops/import` | `{profile, date, mode}` | `{profile, date, mode, counts, stops[]}` |
| POST | `/api/geocode/pin` | `{address_key, lon, lat}` | `{ok, lon, lat, confidence, source}` |
| GET | `/api/fleet` | `?profile=` | `{fleet[], suggested[]}` |
| PUT | `/api/fleet` | `{profile, fleet[]}` | `{fleet[]}` |
| GET | `/api/depot` | `?profile=` | `{depot, suggested}` |
| PUT | `/api/depot` | `{profile, depot}` | `{depot}` |
| POST | `/api/optimize` | `{profile, date, mode, cost_per_km}` | `{run_id, stops[], routes[], unassigned[], totals, comparison, depot}` |
| GET | `/api/runs/{id}` | — | mesmo payload de `/api/optimize` |
| GET | `/api/runs/{id}/romaneio.pdf` | `?vehicle=` | `application/pdf` |
| GET | `/api/runs/{id}/export.csv` | — | `text/csv` |

**Por que `address_key` e não `external_id`:** a chave do cache é o endereço normalizado, não o pedido. Corrigir o pino de um cliente conserta todos os pedidos daquele mesmo endereço de uma vez, hoje e no futuro. O spec §6.3 já reflete isso.

- [ ] **Step 1: Escrever o teste**

```python
# tests/test_api.py
from datetime import date

import pytest
from fastapi.testclient import TestClient

from api.app.main import app

client = TestClient(app)
DIA_LOC = "2026-08-04"
DIA_EP = "2026-08-13"


def test_profiles():
    r = client.get("/api/profiles")
    assert r.status_code == 200
    assert {p["profile"] for p in r.json()} == {"locacao", "entrega_posterior"}


def test_import_com_perfil_invalido_da_422():
    r = client.post("/api/stops/import",
                    json={"profile": "inexistente", "date": DIA_LOC, "mode": "replanejar"})
    assert r.status_code == 422


def test_import_com_data_invalida_da_422():
    r = client.post("/api/stops/import",
                    json={"profile": "locacao", "date": "ontem", "mode": "replanejar"})
    assert r.status_code == 422


def test_run_inexistente_da_404():
    assert client.get("/api/runs/999999").status_code == 404


@pytest.mark.erp
@pytest.mark.slow
def test_import_locacao_devolve_paradas_e_contagens():
    r = client.post("/api/stops/import",
                    json={"profile": "locacao", "date": DIA_LOC, "mode": "replanejar"})
    assert r.status_code == 200
    body = r.json()
    assert body["counts"]["total"] == len(body["stops"])
    assert body["counts"]["total"] > 10
    assert body["counts"]["delivery"] + body["counts"]["pickup"] == body["counts"]["total"]
    assert "pickup_dropped" in body["counts"]        # teto reportado, não engolido
    s = body["stops"][0]
    assert {"external_id", "kind", "cliente_nome", "address", "address_key",
            "lon", "lat", "confidence", "source"} <= set(s)


@pytest.mark.erp
@pytest.mark.slow
def test_taxa_de_geocodificacao_atende_o_criterio_de_sucesso():
    r = client.post("/api/stops/import",
                    json={"profile": "locacao", "date": DIA_LOC, "mode": "replanejar"})
    c = r.json()["counts"]
    bons = c["high"] + c["medium"]
    assert bons / c["total"] >= 0.70, f"geocodificação em {bons}/{c['total']}"


@pytest.mark.erp
@pytest.mark.slow
def test_pin_manual_altera_a_parada_na_proxima_importacao():
    body = client.post("/api/stops/import",
                       json={"profile": "locacao", "date": DIA_LOC,
                             "mode": "replanejar"}).json()
    alvo = body["stops"][0]
    r = client.post("/api/geocode/pin", json={"address_key": alvo["address_key"],
                                              "lon": -54.7000, "lat": -22.1000})
    assert r.status_code == 200 and r.json()["source"] == "manual"

    de_novo = client.post("/api/stops/import",
                          json={"profile": "locacao", "date": DIA_LOC,
                                "mode": "replanejar"}).json()
    mesma = next(s for s in de_novo["stops"]
                 if s["address_key"] == alvo["address_key"])
    assert (mesma["lon"], mesma["lat"]) == (-54.7000, -22.1000)
    assert mesma["source"] == "manual"


@pytest.mark.erp
def test_fleet_sugere_veiculos_do_erp_e_persiste_a_configuracao():
    r = client.get("/api/fleet", params={"profile": "entrega_posterior"})
    assert r.status_code == 200
    assert len(r.json()["suggested"]) > 5           # 37 veículos cadastrados

    novo = [{"id": "CAM1", "label": "Caminhão 1", "placa": "AEY2862",
             "capacity": 12, "trips": 2, "shift_start_s": 25200,
             "shift_end_s": 64800, "enabled": True, "erp_id_veiculo": 5}]
    assert client.put("/api/fleet", json={"profile": "entrega_posterior",
                                          "fleet": novo}).status_code == 200
    guardado = client.get("/api/fleet", params={"profile": "entrega_posterior"}).json()
    assert [v["id"] for v in guardado["fleet"]] == ["CAM1"]
    assert guardado["fleet"][0]["trips"] == 2


@pytest.mark.erp
@pytest.mark.slow
def test_depot_sugerido_vem_do_cadastro_do_estabelecimento():
    r = client.get("/api/depot", params={"profile": "locacao"})
    assert r.status_code == 200
    sug = r.json()["suggested"]
    assert sug is not None and -60 < sug["lon"] < -50


@pytest.mark.erp
@pytest.mark.slow
def test_optimize_devolve_rotas_comparativo_e_persiste_o_run():
    client.put("/api/depot", json={"profile": "locacao", "depot": {
        "label": "Matriz", "lon": -54.8060, "lat": -22.2210,
        "address": "Rua Ponta Porã, 1343"}})
    client.put("/api/fleet", json={"profile": "locacao", "fleet": [
        {"id": "MB", "label": "MB 1513", "placa": "KTD3645", "capacity": 2,
         "trips": 6, "shift_start_s": 25200, "shift_end_s": 68400,
         "enabled": True, "erp_id_veiculo": 2}]})

    r = client.post("/api/optimize", json={"profile": "locacao", "date": DIA_LOC,
                                           "mode": "replanejar", "cost_per_km": 3.5})
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] > 0
    assert len(body["routes"]) >= 1
    assert body["totals"]["distance_km"] > 0
    assert "km_saved" in body["comparison"]
    assert body["comparison"]["approximate"] is True    # baseline de locação

    guardado = client.get(f"/api/runs/{body['run_id']}")
    assert guardado.status_code == 200
    assert guardado.json()["run_id"] == body["run_id"]


@pytest.mark.erp
@pytest.mark.slow
def test_romaneio_e_csv_do_run():
    body = client.post("/api/optimize", json={"profile": "locacao", "date": DIA_LOC,
                                              "mode": "replanejar"}).json()
    veiculo = body["routes"][0]["vehicle_id"]

    pdf = client.get(f"/api/runs/{body['run_id']}/romaneio.pdf",
                     params={"vehicle": veiculo})
    assert pdf.status_code == 200
    assert pdf.headers["content-type"].startswith("application/pdf")
    assert pdf.content[:5] == b"%PDF-"

    csv_r = client.get(f"/api/runs/{body['run_id']}/export.csv")
    assert csv_r.status_code == 200
    assert "veiculo;seq;chegada" in csv_r.text


@pytest.mark.erp
@pytest.mark.slow
def test_romaneio_de_veiculo_inexistente_da_404():
    body = client.post("/api/optimize", json={"profile": "locacao", "date": DIA_LOC,
                                              "mode": "replanejar"}).json()
    r = client.get(f"/api/runs/{body['run_id']}/romaneio.pdf",
                   params={"vehicle": "NAO_EXISTE#9"})
    assert r.status_code == 404
```

- [ ] **Step 2: Rodar — deve falhar**

Run: `docker compose run --rm api pytest tests/test_api.py -v`
Expected: FAIL com `ModuleNotFoundError: No module named 'api.app.service'`

- [ ] **Step 3: Implementar `api/app/service.py`**

Toda a orquestração vive aqui; `main.py` fica só com HTTP. `optimize` re-importa as paradas em vez de guardar sessão: a consulta ao ERP é barata e o geocode vem do cache, então o resultado é idêntico e a API continua sem estado.

```python
from __future__ import annotations
from datetime import date

from .config import ImportMode, PROFILES, Profile, get_settings
from .db.firebird import connect
from .db.local import LocalStore
from .erp.base import build_source
from .geo.geocoder import Geocoder
from .models import Depot, Stop, VehicleConfig, VehicleRoute
from .routing.baseline import compare, measure_baseline
from .routing.optimizer import Optimizer
from .routing.osrm import OsrmClient

_APROX_LOCACAO = ("baseline aproximado: o ERP não registra veículo nem ordem "
                  "para locação; usada a ordem de lançamento")


def store() -> LocalStore:
    s = LocalStore(get_settings().local_db)
    s.init_schema()
    return s


def geocoder() -> Geocoder:
    return Geocoder(get_settings().streets_db, store())


def optimizer() -> Optimizer:
    s = get_settings()
    return Optimizer(s.vroom_url, OsrmClient(s.osrm_url), s.solver_timeout_s)


# ---------------------------------------------------------------- importação
def import_stops(profile: Profile, target_date: date,
                 mode: ImportMode) -> tuple[list[Stop], dict]:
    with connect(profile) as conn:
        source = build_source(profile, conn)
        stops = source.fetch(target_date, mode)
        # Coletas vencidas que não couberam no teto do dia. Vai para a tela:
        # dizer "46 paradas" quando 272 ficaram de fora é mentir para o usuário.
        dropped = getattr(source, "dropped_pickups", 0)

    geo = geocoder()
    for s in stops:
        s.address_key, s.geo = geo.geocode(s.address)

    counts = {
        "total": len(stops),
        "pickup_dropped": dropped,
        "delivery": sum(1 for s in stops if s.kind == "delivery"),
        "pickup": sum(1 for s in stops if s.kind == "pickup"),
        "high": sum(1 for s in stops if s.geo and s.geo.confidence == "high"),
        "medium": sum(1 for s in stops if s.geo and s.geo.confidence == "medium"),
        "low": sum(1 for s in stops if s.geo and s.geo.confidence == "low"),
        "failed": sum(1 for s in stops
                      if not s.geo or s.geo.confidence == "failed"),
    }
    return stops, counts


# ---------------------------------------------------------------- serialização
def stop_payload(s: Stop) -> dict:
    a = s.address
    endereco = ", ".join(p for p in [a.logradouro, a.bairro, a.cidade] if p)
    return {
        "external_id": s.external_id, "kind": s.kind,
        "cliente_id": s.cliente_id, "cliente_nome": s.cliente_nome,
        "address": endereco, "address_key": s.address_key,
        "amount": s.amount, "priority": s.priority,
        "days_overdue": s.days_overdue, "doc": s.doc, "notes": s.notes,
        "lon": s.geo.lon if s.geo else None,
        "lat": s.geo.lat if s.geo else None,
        "confidence": s.geo.confidence if s.geo else "failed",
        "source": s.geo.source if s.geo else "none",
    }


def route_payload(r: VehicleRoute) -> dict:
    return {
        "vehicle_id": r.vehicle_id, "config_id": r.config_id, "label": r.label,
        "trip_index": r.trip_index, "geometry": r.geometry,
        "distance_km": round(r.distance_m / 1000, 1),
        "duration_min": round(r.duration_s / 60),
        "steps": [{"seq": s.seq, "stop_external_id": s.stop_external_id,
                   "kind": s.kind, "lon": s.lon, "lat": s.lat,
                   "arrival_s": s.arrival_s, "load_after": s.load_after}
                  for s in sorted(r.steps, key=lambda x: x.seq)],
    }


# ---------------------------------------------------------------- otimização
def optimize(profile: Profile, target_date: date, mode: ImportMode,
             cost_per_km: float = 3.50) -> dict:
    st = store()
    depot = st.get_depot(profile)
    if depot is None:
        raise ValueError("depósito não configurado para este perfil")
    fleet = [v for v in st.get_fleet(profile) if v.enabled]
    if not fleet:
        raise ValueError("nenhum veículo habilitado na frota")

    stops, counts = import_stops(profile, target_date, mode)

    opt = optimizer()
    solution = opt.solve(stops, fleet, depot)

    # O baseline mede SO as paradas que a solucao atendeu. Comparar 38 paradas
    # em ordem de lancamento contra 23 otimizadas nao mede otimizacao: mede o
    # trabalho que ficou de fora. Medido na base real, isso inflava a economia
    # de algo plausivel para 81,8%.
    atendidas = {s.stop_external_id for r in solution.routes for s in r.steps}
    stops_comparados = [s for s in stops if s.external_id in atendidas]
    with connect(profile) as conn:
        trips = build_source(profile, conn).baseline_order(stops_comparados)
    aproximado = profile is Profile.LOCACAO
    base = measure_baseline(trips, stops_comparados, OsrmClient(get_settings().osrm_url), depot,
                            approximate=aproximado,
                            note=_APROX_LOCACAO if aproximado else "")
    comp = compare(solution, base, cost_per_km=cost_per_km)

    payload = {
        "profile": profile.value,
        "mode": mode.value,
        "counts": counts,
        "depot": {"label": depot.label, "lon": depot.lon, "lat": depot.lat,
                  "address": depot.address},
        "stops": [stop_payload(s) for s in stops],
        "routes": [route_payload(r) for r in solution.routes if r.steps],
        "unassigned": [{"stop_external_id": u.stop_external_id, "reason": u.reason}
                       for u in solution.unassigned],
        "totals": {"distance_km": round(solution.total_distance_m / 1000, 1),
                   "duration_h": round(solution.total_duration_s / 3600, 1),
                   "vehicles_used": len([r for r in solution.routes if r.steps]),
                   # Cobertura fica ao lado do numero. Um plano que deixa 40%
                   # do dia de fora nao e um plano, e a economia so vale para
                   # as paradas efetivamente comparadas.
                   "stops_total": len(stops),
                   "stops_served": len(stops_comparados),
                   "stops_unassigned": len(solution.unassigned)},
        "comparison": comp.__dict__,
    }
    payload["run_id"] = st.save_run(profile, target_date.isoformat(), payload)
    return payload
```

- [ ] **Step 4: Implementar `api/app/main.py`**

```python
from __future__ import annotations
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import service
from .config import ImportMode, PROFILES, Profile
from .db.firebird import connect
from .export.romaneio import build_csv, build_romaneio_pdf
from .models import (Address, Depot, RouteStep, Stop, VehicleConfig, VehicleRoute)
from .routing.vroom import NoGeocodedStops
from .routing.optimizer import VroomError

app = FastAPI(title="Roteirizador Softgran", version="0.1.0")

WEB = Path("/srv/web")
if WEB.exists():
    app.mount("/app", StaticFiles(directory=WEB, html=True), name="web")


# ------------------------------------------------------------------ contratos
class ImportRequest(BaseModel):
    profile: Profile
    date: date
    mode: ImportMode = ImportMode.REPLANEJAR


class OptimizeRequest(ImportRequest):
    cost_per_km: float = Field(default=3.50, gt=0)


class PinRequest(BaseModel):
    address_key: str
    lon: float
    lat: float


class VehicleIn(BaseModel):
    id: str
    label: str
    placa: str | None = None
    capacity: int = Field(default=10, ge=1)
    trips: int = Field(default=1, ge=1, le=20)
    shift_start_s: int = 7 * 3600
    shift_end_s: int = 18 * 3600
    enabled: bool = True
    erp_id_veiculo: int | None = None


class FleetIn(BaseModel):
    profile: Profile
    fleet: list[VehicleIn]


class DepotIn(BaseModel):
    label: str
    lon: float
    lat: float
    address: str = ""


class DepotRequest(BaseModel):
    profile: Profile
    depot: DepotIn


# ------------------------------------------------------------------ endpoints
@app.get("/api/profiles")
def profiles() -> list[dict]:
    return [{"profile": p.value, "label": c.label} for p, c in PROFILES.items()]


@app.post("/api/stops/import")
def import_stops(req: ImportRequest) -> dict:
    stops, counts = service.import_stops(req.profile, req.date, req.mode)
    return {"profile": req.profile.value, "date": req.date.isoformat(),
            "mode": req.mode.value, "counts": counts,
            "stops": [service.stop_payload(s) for s in stops]}


@app.post("/api/geocode/pin")
def pin(req: PinRequest) -> dict:
    g = service.store().pin_geocode(req.address_key, req.lon, req.lat)
    return {"ok": True, "lon": g.lon, "lat": g.lat,
            "confidence": g.confidence, "source": g.source}


@app.get("/api/fleet")
def get_fleet(profile: Profile) -> dict:
    st = service.store()
    with connect(profile) as c:
        rows = c.query("SELECT ID_VEICULO, DESCRICAO, PLACA FROM VEICULO "
                       "ORDER BY ID_VEICULO")
    suggested = [{"erp_id_veiculo": int(r["ID_VEICULO"]),
                  "label": (r["DESCRICAO"] or "").strip(),
                  "placa": (r["PLACA"] or "").strip()} for r in rows]
    return {"fleet": [v.__dict__ for v in st.get_fleet(profile)],
            "suggested": suggested}


@app.put("/api/fleet")
def put_fleet(req: FleetIn) -> dict:
    fleet = [VehicleConfig(**v.model_dump()) for v in req.fleet]
    service.store().put_fleet(req.profile, fleet)
    return {"fleet": [v.__dict__ for v in fleet]}


@app.get("/api/depot")
def get_depot(profile: Profile) -> dict:
    st = service.store()
    atual = st.get_depot(profile)
    sugerido = None
    with connect(profile) as c:
        rows = c.query("SELECT FIRST 1 NOME, ENDERECO, NUMERO, BAIRRO, CIDADE, UF, CEP"
                       " FROM CLIFOR WHERE FLAG_ESTAB_GERAL = 1")
    if rows:
        r = rows[0]
        addr = Address(logradouro=(r["ENDERECO"] or "").strip() or None,
                       numero=(str(r["NUMERO"]).strip() if r["NUMERO"] else None),
                       bairro=(r["BAIRRO"] or "").strip() or None,
                       cidade=(r["CIDADE"] or "").strip() or None,
                       uf=(r["UF"] or "").strip() or None,
                       cep=(r["CEP"] or "").strip() or None,
                       raw=(r["ENDERECO"] or "").strip())
        _, g = service.geocoder().geocode(addr)
        if g.confidence != "failed":
            sugerido = {"label": (r["NOME"] or "Matriz").strip(),
                        "lon": g.lon, "lat": g.lat,
                        "address": addr.raw, "confidence": g.confidence}
    return {"depot": atual.__dict__ if atual else None, "suggested": sugerido}


@app.put("/api/depot")
def put_depot(req: DepotRequest) -> dict:
    d = Depot(**req.depot.model_dump())
    service.store().put_depot(req.profile, d)
    return {"depot": d.__dict__}


@app.post("/api/optimize")
def optimize(req: OptimizeRequest) -> dict:
    try:
        return service.optimize(req.profile, req.date, req.mode, req.cost_per_km)
    except NoGeocodedStops as exc:
        raise HTTPException(422, str(exc)) from exc
    except (ValueError, VroomError) as exc:
        raise HTTPException(400, str(exc)) from exc


def _load_run(run_id: int) -> dict:
    run = service.store().get_run(run_id)
    if run is None:
        raise HTTPException(404, "execução não encontrada")
    return run


@app.get("/api/runs/{run_id}")
def get_run(run_id: int) -> dict:
    return _load_run(run_id)


def _rebuild(run: dict) -> tuple[list[Stop], list[VehicleRoute], Depot]:
    stops = [Stop(external_id=s["external_id"], kind=s["kind"],
                  cliente_id=s["cliente_id"], cliente_nome=s["cliente_nome"],
                  address=Address(s["address"], None, None, None, None, None,
                                  s["address"]),
                  doc=s["doc"], notes=s["notes"] or "")
             for s in run["stops"]]
    routes = [VehicleRoute(
        vehicle_id=r["vehicle_id"], config_id=r["config_id"], label=r["label"],
        trip_index=r["trip_index"], geometry=r["geometry"],
        distance_m=int(r["distance_km"] * 1000), duration_s=int(r["duration_min"] * 60),
        steps=[RouteStep(seq=s["seq"], stop_external_id=s["stop_external_id"],
                         kind=s["kind"], lon=s["lon"], lat=s["lat"],
                         arrival_s=s["arrival_s"], load_after=s["load_after"])
               for s in r["steps"]]) for r in run["routes"]]
    d = run["depot"]
    return stops, routes, Depot(d["label"], d["lon"], d["lat"], d["address"])


@app.get("/api/runs/{run_id}/romaneio.pdf")
def romaneio(run_id: int, vehicle: str = Query(...)) -> Response:
    run = _load_run(run_id)
    stops, routes, depot = _rebuild(run)
    rota = next((r for r in routes if r.vehicle_id == vehicle), None)
    if rota is None:
        raise HTTPException(404, f"veículo {vehicle} não está nesta execução")
    pdf = build_romaneio_pdf(rota, stops, depot,
                             PROFILES[Profile(run["profile"])].label,
                             run["target_date"])
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition":
            f'inline; filename="romaneio-{vehicle.replace("#", "-")}.pdf"'})


@app.get("/api/runs/{run_id}/export.csv")
def export_csv(run_id: int) -> Response:
    run = _load_run(run_id)
    stops, routes, _ = _rebuild(run)
    return Response(build_csv(routes, stops), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="rotas-{run_id}.csv"'})


@app.get("/")
def raiz() -> Response:
    return FileResponse(WEB / "index.html") if (WEB / "index.html").exists() \
        else Response("Roteirizador no ar. UI em /app", media_type="text/plain")
```

- [ ] **Step 5: Rodar os testes rápidos**

Run: `docker compose run --rm api pytest tests/test_api.py -v -m "not erp and not slow"`
Expected: 4 passed

- [ ] **Step 6: Rodar os testes que tocam ERP e stack**

Run: `docker compose run --rm api pytest tests/test_api.py -v`
Expected: 12 passed

Se `test_taxa_de_geocodificacao_atende_o_criterio_de_sucesso` falhar, **não relaxar o limite** — investigar quais endereços caíram em `failed`/`low` e ajustar `geo/normalize.py`, que é onde o problema estará:

```bash
docker compose run --rm api python -c "
from datetime import date
from api.app.config import ImportMode, Profile
from api.app import service
stops, c = service.import_stops(Profile.LOCACAO, date(2026,8,4), ImportMode.REPLANEJAR)
print(c)
for s in stops:
    if not s.geo or s.geo.confidence in ('low','failed'):
        print(s.geo.source if s.geo else 'none', '|', s.address.raw)
"
```

- [ ] **Step 7: Commit**

```bash
git add api/app/service.py api/app/main.py tests/test_api.py
git commit -m "feat: api http de importacao, geocode, frota, otimizacao e exportacao"
```

---

## Task 14: Interface web com mapa

**Files:**
- Create: `web/index.html`, `web/app.js`, `web/style.css`, `web/vendor/leaflet.js`, `web/vendor/leaflet.css`
- Test: `tests/test_web.py` + checklist de verificação manual

**Interfaces:**
- Consumes: os endpoints da Task 13
- Produces: SPA servida em `/app`

- [ ] **Step 1: Vendorizar o Leaflet**

CDN não entra: a demo pode rodar sem internet, e o spec exige `docker compose up` funcionando do zero.

```bash
mkdir -p web/vendor
curl -fL -o web/vendor/leaflet.js  https://unpkg.com/leaflet@1.9.4/dist/leaflet.js
curl -fL -o web/vendor/leaflet.css https://unpkg.com/leaflet@1.9.4/dist/leaflet.css
curl -fL -o web/vendor/marker-shadow.png https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png
ls -la web/vendor
```

- [ ] **Step 2: Escrever `web/index.html`**

```html
<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Roteirizador — Softgran</title>
  <link rel="stylesheet" href="vendor/leaflet.css">
  <link rel="stylesheet" href="style.css">
</head>
<body>
<header>
  <h1>Roteirizador</h1>
  <div class="controls">
    <select id="profile"></select>
    <input type="date" id="date" value="2026-08-04">
    <select id="mode">
      <option value="replanejar">Replanejar dia histórico</option>
      <option value="producao">Produção (pendentes)</option>
    </select>
    <button id="btn-import">Importar</button>
    <button id="btn-optimize" class="primary" disabled>Otimizar</button>
    <button id="btn-fleet">Frota</button>
  </div>
</header>

<main>
  <aside id="sidebar">
    <div id="counts" class="counts"></div>
    <ul id="stops"></ul>
  </aside>
  <div id="map"></div>
</main>

<section id="panel" hidden>
  <h2>Hoje vs Otimizado</h2>
  <div id="kpis" class="kpis"></div>
  <p id="note" class="note"></p>
  <div id="routes" class="routes"></div>
  <div id="unassigned" class="unassigned"></div>
</section>

<dialog id="fleet-dialog">
  <h2>Frota e depósito</h2>
  <div id="depot-box"></div>
  <table id="fleet-table">
    <thead><tr><th>Usar</th><th>Veículo</th><th>Placa</th><th>Cap.</th>
      <th>Viagens</th><th>Início</th><th>Fim</th></tr></thead>
    <tbody></tbody>
  </table>
  <menu>
    <button id="fleet-cancel">Fechar</button>
    <button id="fleet-save" class="primary">Salvar</button>
  </menu>
</dialog>

<script src="vendor/leaflet.js"></script>
<script src="app.js"></script>
</body>
</html>
```

- [ ] **Step 3: Escrever `web/style.css`**

```css
:root{--bg:#0f1720;--panel:#17212e;--line:#27364a;--txt:#e6edf5;--muted:#93a4bb;
      --hi:#3ddc84;--med:#f5b942;--low:#e8663d;--fail:#c0392b;--acc:#4d9de0}
*{box-sizing:border-box}
body{margin:0;font:14px/1.45 "Segoe UI",system-ui,sans-serif;background:var(--bg);
     color:var(--txt);height:100vh;display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:16px;padding:10px 16px;
       background:var(--panel);border-bottom:1px solid var(--line)}
header h1{font-size:16px;margin:0;letter-spacing:.5px}
.controls{display:flex;gap:8px;flex-wrap:wrap}
select,input,button{background:#101b26;color:var(--txt);border:1px solid var(--line);
                    border-radius:6px;padding:6px 10px;font:inherit}
button{cursor:pointer}
button:disabled{opacity:.45;cursor:not-allowed}
button.primary{background:var(--acc);border-color:var(--acc);color:#04121f;font-weight:600}
main{flex:1;display:flex;min-height:0}
#sidebar{width:340px;overflow:auto;border-right:1px solid var(--line);background:var(--panel)}
#map{flex:1}
.counts{display:flex;flex-wrap:wrap;gap:6px;padding:10px;border-bottom:1px solid var(--line)}
.chip{padding:2px 8px;border-radius:99px;font-size:12px;background:#101b26;
      border:1px solid var(--line)}
.chip.high{border-color:var(--hi);color:var(--hi)}
.chip.medium{border-color:var(--med);color:var(--med)}
.chip.low{border-color:var(--low);color:var(--low)}
.chip.failed{border-color:var(--fail);color:var(--fail)}
#stops{list-style:none;margin:0;padding:0}
#stops li{padding:8px 10px;border-bottom:1px solid var(--line);cursor:pointer}
#stops li:hover{background:#101b26}
#stops li b{display:block;font-weight:600}
#stops li small{color:var(--muted)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.dot.high{background:var(--hi)}.dot.medium{background:var(--med)}
.dot.low{background:var(--low)}.dot.failed{background:var(--fail)}
#panel{background:var(--panel);border-top:1px solid var(--line);padding:12px 16px;
       max-height:38vh;overflow:auto}
#panel h2{margin:0 0 10px;font-size:15px}
.kpis{display:flex;gap:20px;flex-wrap:wrap}
.kpi{background:#101b26;border:1px solid var(--line);border-radius:8px;padding:10px 14px;
     min-width:150px}
.kpi span{display:block;color:var(--muted);font-size:12px}
.kpi strong{font-size:22px;font-variant-numeric:tabular-nums}
.kpi.good strong{color:var(--hi)}
.note{color:var(--med);font-size:12px;margin:10px 0 0}
.routes{margin-top:12px;display:flex;flex-direction:column;gap:6px}
.route-row{display:flex;align-items:center;gap:10px;padding:6px 8px;background:#101b26;
           border:1px solid var(--line);border-radius:6px}
.swatch{width:14px;height:14px;border-radius:3px;flex:0 0 auto}
.route-row a{color:var(--acc);margin-left:auto}
.unassigned{margin-top:12px;color:var(--low);font-size:13px}
dialog{background:var(--panel);color:var(--txt);border:1px solid var(--line);
       border-radius:10px;min-width:640px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:5px 6px;border-bottom:1px solid var(--line);text-align:left}
td input[type=number]{width:70px}
menu{display:flex;gap:8px;justify-content:flex-end;padding:12px 0 0;margin:0}
.leaflet-container{background:#0a1119}
```

- [ ] **Step 4: Escrever `web/app.js`**

O decodificador de polyline é uma função de ~15 linhas em vez de mais uma dependência. Note a inversão `(lon,lat) → (lat,lng)` na fronteira com o Leaflet — é a única no front (constraint global).

```javascript
const CORES = ["#4d9de0","#3ddc84","#f5b942","#e8663d","#b06fdb","#3dd6c4",
               "#e05c8a","#8fd44a"];
const $ = (id) => document.getElementById(id);

const state = {stops: [], run: null, markers: new Map(), layers: []};

const map = L.map("map").setView([-22.2210, -54.8060], 13);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
  {maxZoom: 19, attribution: "© OpenStreetMap"}).addTo(map);

// Polyline5 do OSRM -> [[lat, lng], ...]
function decodePolyline(str) {
  let index = 0, lat = 0, lng = 0; const out = [];
  while (index < str.length) {
    let shift = 0, result = 0, b;
    do { b = str.charCodeAt(index++) - 63; result |= (b & 0x1f) << shift; shift += 5; }
    while (b >= 0x20);
    lat += (result & 1) ? ~(result >> 1) : (result >> 1);
    shift = 0; result = 0;
    do { b = str.charCodeAt(index++) - 63; result |= (b & 0x1f) << shift; shift += 5; }
    while (b >= 0x20);
    lng += (result & 1) ? ~(result >> 1) : (result >> 1);
    out.push([lat / 1e5, lng / 1e5]);
  }
  return out;
}

async function api(path, opts) {
  const r = await fetch(path, {headers: {"Content-Type": "application/json"}, ...opts});
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

const body = () => ({profile: $("profile").value, date: $("date").value,
                     mode: $("mode").value});

function clearLayers() {
  state.layers.forEach((l) => map.removeLayer(l));
  state.layers = [];
  state.markers.forEach((m) => map.removeLayer(m));
  state.markers.clear();
}

function pinIcon(color, texto) {
  return L.divIcon({className: "", iconSize: [22, 22], iconAnchor: [11, 11],
    html: `<div style="width:22px;height:22px;border-radius:50%;background:${color};
      border:2px solid #0f1720;color:#04121f;font:600 11px/18px system-ui;
      text-align:center">${texto}</div>`});
}

const CORDE = {high: "#3ddc84", medium: "#f5b942", low: "#e8663d", failed: "#c0392b"};

function drawStops(stops) {
  clearLayers();
  const pts = [];
  stops.forEach((s) => {
    if (s.lat == null) return;
    const m = L.marker([s.lat, s.lon], {
      draggable: true, icon: pinIcon(CORDE[s.confidence], s.kind === "pickup" ? "↑" : "↓"),
    }).addTo(map);
    m.bindPopup(`<b>${s.cliente_nome}</b><br>${s.address}<br>
      <small>${s.kind === "pickup" ? "Coleta" : "Entrega"} ·
      ${s.confidence} (${s.source})${s.days_overdue ? ` · ${s.days_overdue}d atraso` : ""}
      </small><br><small>arraste o pino para corrigir</small>`);
    m.on("dragend", async (e) => {
      const {lat, lng} = e.target.getLatLng();
      await api("/api/geocode/pin", {method: "POST",
        body: JSON.stringify({address_key: s.address_key, lon: lng, lat})});
      s.lat = lat; s.lon = lng; s.confidence = "high"; s.source = "manual";
      m.setIcon(pinIcon(CORDE.high, s.kind === "pickup" ? "↑" : "↓"));
      renderCounts();
    });
    state.markers.set(s.external_id, m);
    pts.push([s.lat, s.lon]);
  });
  if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.15));
}

function renderCounts() {
  const c = {total: state.stops.length, delivery: 0, pickup: 0,
             high: 0, medium: 0, low: 0, failed: 0};
  state.stops.forEach((s) => { c[s.kind]++; c[s.confidence]++; });
  $("counts").innerHTML = `
    <span class="chip">${c.total} paradas</span>
    <span class="chip">${c.delivery} entregas</span>
    <span class="chip">${c.pickup} coletas</span>
    <span class="chip high">${c.high} alta</span>
    <span class="chip medium">${c.medium} média</span>
    <span class="chip low">${c.low} baixa</span>
    <span class="chip failed">${c.failed} sem local</span>`;
}

function renderStops() {
  $("stops").innerHTML = state.stops.map((s) => `
    <li data-id="${s.external_id}">
      <b><span class="dot ${s.confidence}"></span>${s.cliente_nome}</b>
      <small>${s.kind === "pickup" ? "COLETA" : "ENTREGA"} · ${s.address || "sem endereço"}</small>
    </li>`).join("");
  $("stops").querySelectorAll("li").forEach((li) => li.onclick = () => {
    const m = state.markers.get(li.dataset.id);
    if (m) { map.setView(m.getLatLng(), 16); m.openPopup(); }
  });
}

$("btn-import").onclick = async () => {
  $("btn-import").disabled = true;
  try {
    const r = await api("/api/stops/import", {method: "POST", body: JSON.stringify(body())});
    state.stops = r.stops;
    renderCounts(); renderStops(); drawStops(r.stops);
    $("btn-optimize").disabled = r.stops.length === 0;
    $("panel").hidden = true;
  } catch (e) { alert("Falha ao importar: " + e.message); }
  finally { $("btn-import").disabled = false; }
};

$("btn-optimize").onclick = async () => {
  $("btn-optimize").disabled = true;
  $("btn-optimize").textContent = "Otimizando…";
  try {
    const run = await api("/api/optimize", {method: "POST",
      body: JSON.stringify({...body(), cost_per_km: 3.5})});
    state.run = run; state.stops = run.stops;
    renderCounts(); renderStops(); drawStops(run.stops);
    drawRoutes(run); renderPanel(run);
  } catch (e) { alert("Falha ao otimizar: " + e.message); }
  finally { $("btn-optimize").disabled = false; $("btn-optimize").textContent = "Otimizar"; }
};

function drawRoutes(run) {
  run.routes.forEach((r, i) => {
    const cor = CORES[i % CORES.length];
    if (r.geometry) {
      const l = L.polyline(decodePolyline(r.geometry),
        {color: cor, weight: 4, opacity: .85}).addTo(map);
      state.layers.push(l);
    }
    r.steps.forEach((st) => {
      const m = state.markers.get(st.stop_external_id);
      if (m) m.setIcon(pinIcon(cor, String(st.seq)));
    });
  });
  const d = run.depot;
  state.layers.push(L.circleMarker([d.lat, d.lon],
    {radius: 9, color: "#fff", fillColor: "#111", fillOpacity: 1})
    .bindPopup(`<b>${d.label}</b><br>${d.address}`).addTo(map));
}

function renderPanel(run) {
  const c = run.comparison;
  const bom = c.km_saved > 0;
  $("kpis").innerHTML = `
    <div class="kpi"><span>Rota atual</span><strong>${c.baseline_km} km</strong></div>
    <div class="kpi"><span>Rota otimizada</span><strong>${c.optimized_km} km</strong></div>
    <div class="kpi ${bom ? "good" : ""}"><span>Economia</span>
      <strong>${c.km_saved} km (${c.percent_km_saved}%)</strong></div>
    <div class="kpi ${bom ? "good" : ""}"><span>Horas poupadas/dia</span>
      <strong>${c.hours_saved} h</strong></div>
    <div class="kpi ${bom ? "good" : ""}"><span>Economia mensal</span>
      <strong>R$ ${c.monthly_brl_saved.toLocaleString("pt-BR")}</strong></div>
    <div class="kpi"><span>Veículos/viagens</span>
      <strong>${run.totals.vehicles_used}</strong></div>`;
  $("note").textContent = c.approximate ? `⚠ ${c.note}` : "";

  $("routes").innerHTML = run.routes.map((r, i) => `
    <div class="route-row">
      <span class="swatch" style="background:${CORES[i % CORES.length]}"></span>
      <b>${r.label}</b>
      <span>${r.steps.length} paradas · ${r.distance_km} km · ${r.duration_min} min</span>
      <a href="/api/runs/${run.run_id}/romaneio.pdf?vehicle=${encodeURIComponent(r.vehicle_id)}"
         target="_blank">romaneio PDF</a>
    </div>`).join("") +
    `<div class="route-row"><a href="/api/runs/${run.run_id}/export.csv">baixar CSV</a></div>`;

  $("unassigned").innerHTML = run.unassigned.length
    ? `<b>${run.unassigned.length} paradas não atendidas:</b> ` +
      run.unassigned.map((u) => `${u.stop_external_id} (${u.reason})`).join("; ")
    : "";
  $("panel").hidden = false;
}

// ---- frota e depósito ------------------------------------------------------
const hhmm = (s) => `${String(Math.floor(s / 3600)).padStart(2, "0")}:` +
                    `${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}`;
const secs = (v) => { const [h, m] = v.split(":").map(Number); return h * 3600 + m * 60; };

$("btn-fleet").onclick = async () => {
  const profile = $("profile").value;
  const [f, d] = await Promise.all([
    api(`/api/fleet?profile=${profile}`), api(`/api/depot?profile=${profile}`)]);

  const dep = d.depot || d.suggested ||
    {label: "Depósito", lon: -54.8060, lat: -22.2210, address: ""};
  $("depot-box").innerHTML = `<p><b>Depósito:</b>
    <input id="dep-label" value="${dep.label}">
    <input id="dep-lon" type="number" step="0.0001" value="${dep.lon}">
    <input id="dep-lat" type="number" step="0.0001" value="${dep.lat}">
    ${d.depot ? "" : "<em>(sugerido pelo cadastro — confira)</em>"}</p>`;

  const salvos = new Map(f.fleet.map((v) => [String(v.erp_id_veiculo ?? v.id), v]));
  const linhas = f.suggested.map((s) => {
    const v = salvos.get(String(s.erp_id_veiculo)) || {};
    return {id: v.id || `V${s.erp_id_veiculo}`, label: v.label || s.label,
            placa: v.placa || s.placa, capacity: v.capacity ?? 10,
            trips: v.trips ?? 1, shift_start_s: v.shift_start_s ?? 25200,
            shift_end_s: v.shift_end_s ?? 64800, enabled: v.enabled ?? false,
            erp_id_veiculo: s.erp_id_veiculo};
  });
  $("fleet-table").querySelector("tbody").innerHTML = linhas.map((v, i) => `
    <tr data-i="${i}">
      <td><input type="checkbox" class="f-on" ${v.enabled ? "checked" : ""}></td>
      <td><input class="f-label" value="${v.label}"></td>
      <td>${v.placa || ""}</td>
      <td><input type="number" class="f-cap" min="1" value="${v.capacity}"></td>
      <td><input type="number" class="f-trips" min="1" max="20" value="${v.trips}"></td>
      <td><input type="time" class="f-ini" value="${hhmm(v.shift_start_s)}"></td>
      <td><input type="time" class="f-fim" value="${hhmm(v.shift_end_s)}"></td>
    </tr>`).join("");
  $("fleet-dialog").dataset.base = JSON.stringify(linhas);
  $("fleet-dialog").showModal();
};

$("fleet-cancel").onclick = () => $("fleet-dialog").close();

$("fleet-save").onclick = async () => {
  const profile = $("profile").value;
  const base = JSON.parse($("fleet-dialog").dataset.base);
  const fleet = [...$("fleet-table").querySelectorAll("tbody tr")].map((tr) => {
    const v = base[+tr.dataset.i], q = (c) => tr.querySelector(c);
    return {...v, enabled: q(".f-on").checked, label: q(".f-label").value,
            capacity: +q(".f-cap").value, trips: +q(".f-trips").value,
            shift_start_s: secs(q(".f-ini").value), shift_end_s: secs(q(".f-fim").value)};
  }).filter((v) => v.enabled);

  await api("/api/depot", {method: "PUT", body: JSON.stringify({profile, depot: {
    label: $("dep-label").value, lon: +$("dep-lon").value,
    lat: +$("dep-lat").value, address: ""}})});
  await api("/api/fleet", {method: "PUT", body: JSON.stringify({profile, fleet})});
  $("fleet-dialog").close();
};

// ---- bootstrap -------------------------------------------------------------
(async () => {
  const ps = await api("/api/profiles");
  $("profile").innerHTML = ps.map((p) =>
    `<option value="${p.profile}">${p.label}</option>`).join("");
})();
```

- [ ] **Step 5: Escrever `tests/test_web.py`**

```python
from fastapi.testclient import TestClient
from api.app.main import app

client = TestClient(app)


def test_index_servido_em_app():
    r = client.get("/app/")
    assert r.status_code == 200
    assert "Roteirizador" in r.text


def test_leaflet_vendorizado_e_nao_vem_de_cdn():
    assert client.get("/app/vendor/leaflet.js").status_code == 200
    assert client.get("/app/vendor/leaflet.css").status_code == 200
    html = client.get("/app/").text
    assert "unpkg.com" not in html and "cdn." not in html


def test_app_js_e_css_servidos():
    assert client.get("/app/app.js").status_code == 200
    assert client.get("/app/style.css").status_code == 200


def test_raiz_responde():
    assert client.get("/").status_code == 200
```

- [ ] **Step 6: Rodar os testes**

Run: `docker compose run --rm api pytest tests/test_web.py -v`
Expected: 4 passed

- [ ] **Step 7: Verificação manual no navegador**

Run: `docker compose up -d && start http://localhost:8000/app/`

Conferir, na ordem:
1. O seletor de perfil traz "Locação de equipamentos" e "Entrega posterior".
2. **Frota** → o diálogo lista os veículos do ERP; o depósito vem preenchido com a sugestão do cadastro. Marcar 1 caminhão para locação (capacidade 2, 6 viagens), salvar.
3. Perfil `locacao`, data `2026-08-04`, modo `replanejar` → **Importar**. Pinos aparecem no mapa coloridos por confiança; a barra de contagens soma o total.
4. Arrastar um pino vermelho → ele fica verde e a contagem muda. Reimportar → ele continua no lugar novo.
5. **Otimizar** → linhas coloridas por veículo, pinos renumerados pela sequência, painel inferior com os KPIs e o aviso de baseline aproximado.
6. Clicar em "romaneio PDF" abre o PDF com a lista ordenada; "baixar CSV" baixa o arquivo.
7. Trocar para `entrega_posterior`, data `2024-12-09` → importar e otimizar o dia de pico (215 paradas).

- [ ] **Step 8: Commit**

```bash
git add web/ tests/test_web.py
git commit -m "feat: interface web com mapa, correcao de pino e painel hoje-vs-otimizado"
```

---

## Task 15: Verificação ponta a ponta e preparo da demo

**Files:**
- Create: `tests/test_e2e.py`, `scripts/seed_demo.py`, `README.md`
- Test: `tests/test_e2e.py`

**Interfaces:**
- Consumes: tudo
- Produces: `seed_demo.py` que configura frota e depósito dos dois perfis; `README.md` com o roteiro da demo

- [ ] **Step 1: Escrever `scripts/seed_demo.py`**

A frota é semeada aqui porque nenhuma das duas bases tem capacidade preenchida em `VEICULO`, e o cliente de locação só tem 2 registros, um deles fictício (`GERAL`).

```python
"""Configura frota e depósito para a demo dos dois perfis.
Rodar: docker compose run --rm api python scripts/seed_demo.py"""
from api.app.config import Profile
from api.app.models import Depot, VehicleConfig
from api.app import service

st = service.store()

# Dourados-MS. Conferir no mapa e ajustar arrastando, se necessário.
st.put_depot(Profile.LOCACAO,
             Depot("Matriz — Locação", -54.8060, -22.2210, "Rua Ponta Porã, 1343"))
st.put_depot(Profile.ENTREGA_POSTERIOR,
             Depot("Matriz — Depósito", -54.8120, -22.2260, "Av. Marcelino Pires"))

# Poliguindaste: 1 caçamba por vez -> capacidade 1, várias viagens no dia.
st.put_fleet(Profile.LOCACAO, [
    VehicleConfig(id="MB1513", label="MB 1513", placa="KTD-3645", capacity=1,
                  trips=8, shift_start_s=7 * 3600, shift_end_s=18 * 3600,
                  erp_id_veiculo=2),
    VehicleConfig(id="TRUCK2", label="Truck reserva", placa="—", capacity=1,
                  trips=6, shift_start_s=7 * 3600, shift_end_s=17 * 3600),
])

# Caminhões de material de construção: carga fracionada, 1 viagem longa.
st.put_fleet(Profile.ENTREGA_POSTERIOR, [
    VehicleConfig(id="AEY2862", label="CAM BRANCO", placa="AEY-2862", capacity=14,
                  trips=2, shift_start_s=7 * 3600, shift_end_s=18 * 3600,
                  erp_id_veiculo=5),
    VehicleConfig(id="HQY8879", label="F4000", placa="HQY-8879", capacity=10,
                  trips=2, shift_start_s=7 * 3600, shift_end_s=18 * 3600,
                  erp_id_veiculo=6),
    VehicleConfig(id="IGD9824", label="TRUCK", placa="IGD-9824", capacity=20,
                  trips=2, shift_start_s=7 * 3600, shift_end_s=18 * 3600,
                  erp_id_veiculo=21),
    VehicleConfig(id="HRY0575", label="CAM BRANCO W8150", placa="HRY-0575",
                  capacity=14, trips=2, shift_start_s=7 * 3600,
                  shift_end_s=18 * 3600, erp_id_veiculo=33),
    VehicleConfig(id="RJR3J05", label="SAVEIRO", placa="RJR-3J05", capacity=4,
                  trips=3, shift_start_s=8 * 3600, shift_end_s=17 * 3600,
                  erp_id_veiculo=40),
])

for p in (Profile.LOCACAO, Profile.ENTREGA_POSTERIOR):
    print(p.value, "->", st.get_depot(p).label,
          "|", len(st.get_fleet(p)), "veículos")
```

- [ ] **Step 2: Rodar o seed**

Run: `docker compose run --rm api python scripts/seed_demo.py`
Expected: duas linhas confirmando depósito e contagem de veículos

- [ ] **Step 3: Escrever `tests/test_e2e.py`**

Estes testes verificam os critérios de sucesso do spec §10, contra as duas bases reais.

```python
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from api.app.main import app

client = TestClient(app)
LOC_DIA = "2026-08-04"
EP_PICO = "2024-12-09"
EP_ULTIMO = "2026-08-13"

pytestmark = [pytest.mark.erp, pytest.mark.slow, pytest.mark.stack]


def _otimizar(profile, dia):
    inicio = time.monotonic()
    r = client.post("/api/optimize", json={"profile": profile, "date": dia,
                                           "mode": "replanejar", "cost_per_km": 3.5})
    assert r.status_code == 200, r.text
    return r.json(), time.monotonic() - inicio


# --- critério 1: dia real de cada base em menos de 30 s ---------------------
def test_locacao_roteiriza_em_menos_de_30s():
    run, dt = _otimizar("locacao", LOC_DIA)
    assert dt < 30, f"levou {dt:.1f}s"
    assert len(run["routes"]) >= 1


def test_entrega_posterior_roteiriza_em_menos_de_30s():
    run, dt = _otimizar("entrega_posterior", EP_ULTIMO)
    assert dt < 30, f"levou {dt:.1f}s"


def test_dia_de_pico_215_paradas_roteiriza_em_menos_de_30s():
    run, dt = _otimizar("entrega_posterior", EP_PICO)
    assert dt < 30, f"levou {dt:.1f}s"
    assert run["counts"]["total"] > 190


# --- critério 2: >= 70% em high+medium --------------------------------------
@pytest.mark.parametrize("profile,dia", [("locacao", LOC_DIA),
                                         ("entrega_posterior", EP_ULTIMO)])
def test_taxa_de_geocodificacao(profile, dia):
    c = client.post("/api/stops/import", json={"profile": profile, "date": dia,
                                               "mode": "replanejar"}).json()["counts"]
    bons = c["high"] + c["medium"]
    assert bons / c["total"] >= 0.70, f"{profile}: {bons}/{c['total']}"


# --- critério 3: rotas desenháveis ------------------------------------------
def test_toda_rota_tem_geometria_e_sequencia_continua():
    run, _ = _otimizar("locacao", LOC_DIA)
    for r in run["routes"]:
        assert r["geometry"], f"{r['vehicle_id']} sem geometria"
        assert [s["seq"] for s in r["steps"]] == list(range(1, len(r["steps"]) + 1))
        assert all(s["lat"] and s["lon"] for s in r["steps"])


def test_capacidade_nunca_e_estourada():
    run, _ = _otimizar("locacao", LOC_DIA)
    for r in run["routes"]:
        assert all(s["load_after"] <= 1 for s in r["steps"]), r["vehicle_id"]


def test_nenhuma_parada_some_entre_importacao_e_solucao():
    run, _ = _otimizar("locacao", LOC_DIA)
    atendidas = {s["stop_external_id"] for r in run["routes"] for s in r["steps"]}
    nao = {u["stop_external_id"] for u in run["unassigned"]}
    assert atendidas | nao == {s["external_id"] for s in run["stops"]}


# --- critério 4: comparativo -------------------------------------------------
def test_comparativo_tem_todos_os_numeros():
    run, _ = _otimizar("entrega_posterior", EP_ULTIMO)
    c = run["comparison"]
    assert set(c) >= {"baseline_km", "optimized_km", "km_saved", "hours_saved",
                      "percent_km_saved", "monthly_brl_saved", "approximate", "note"}
    assert c["baseline_km"] > 0
    assert c["approximate"] is False        # baseline real: veículo e hora do ERP


def test_baseline_de_locacao_vem_marcado_como_aproximado():
    run, _ = _otimizar("locacao", LOC_DIA)
    assert run["comparison"]["approximate"] is True
    assert run["comparison"]["note"]


# --- critérios 5 e 6: exportação e aprendizado do cache ----------------------
def test_romaneio_e_csv_de_todos_os_veiculos():
    run, _ = _otimizar("locacao", LOC_DIA)
    for r in run["routes"]:
        pdf = client.get(f"/api/runs/{run['run_id']}/romaneio.pdf",
                         params={"vehicle": r["vehicle_id"]})
        assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"
    assert client.get(f"/api/runs/{run['run_id']}/export.csv").status_code == 200


def test_cache_acelera_a_segunda_importacao():
    corpo = {"profile": "locacao", "date": LOC_DIA, "mode": "replanejar"}
    client.post("/api/stops/import", json=corpo)          # aquece
    t0 = time.monotonic()
    client.post("/api/stops/import", json=corpo)
    assert time.monotonic() - t0 < 5
```

- [ ] **Step 4: Rodar a suíte inteira**

Run: `docker compose run --rm api pytest -v`
Expected: todos verdes.

Se `test_taxa_de_geocodificacao` reprovar, **reportar o número real** e listar os endereços que caíram em `failed`/`low` (comando do Step 6 da Task 13) antes de decidir se ajusta a normalização ou se declara a limitação. Não relaxar o limite sem dizer isso explicitamente.

- [ ] **Step 5: Escrever `README.md`**

````markdown
# Roteirizador — MVP

Roteirização de entregas e coletas a partir do ERP Softgran Empresarial.
Dois módulos atendidos pelo mesmo motor: **locação de equipamentos** (caçambas —
leva equipamento novo, traz o vencido) e **entrega posterior** (venda no balcão
com entrega agendada, mais devoluções).

## Stack

| Peça | Papel |
|---|---|
| OSRM | matriz de distâncias e geometria das rotas |
| VROOM | solver VRP — quem leva o quê, em que ordem |
| Firebird 3 | bases dos clientes, **acesso somente leitura** |
| FastAPI + SQLite | integração, geocodificação, estado próprio |
| Leaflet | mapa |

Geocodificação é feita por um índice de ruas construído do próprio `.pbf` do OSM,
sem depender de serviço externo. O ERP não tem nenhum campo de coordenada.

## Subir do zero

```bash
powershell -File scripts/copy_fdb.ps1      # cópias dos .fdb (não usar os originais)
./scripts/prepare_osm.sh                   # 20-40 min: baixa e processa o mapa
docker compose up -d
docker compose run --rm api python -c "
from pathlib import Path
from api.app.geo.index_builder import build_street_index
print(build_street_index(Path('/srv/data/osm/regiao.osm.pbf'), Path('/srv/data/streets.db')))"
docker compose run --rm api python scripts/seed_demo.py
```

Abrir http://localhost:8000/app/

## Testes

```bash
docker compose run --rm api pytest -v                       # tudo
docker compose run --rm api pytest -m "not erp and not stack"  # só unitários
```

## Roteiro da demo

1. **Locação, 04/08/2026** — importar. Mostrar os pinos por confiança de
   geocodificação e arrastar um vermelho para corrigir: a correção é permanente
   e vale para todos os pedidos daquele endereço.
2. Otimizar. Falar do caso poliguindaste: capacidade 1, 8 viagens no dia,
   entregas e coletas alternadas — o caminhão nunca volta vazio.
3. Painel **Hoje vs Otimizado**: km, horas e R$/mês. Dizer que o baseline de
   locação é aproximado (o ERP não registra ordem) — isso constrói credibilidade.
4. **Entrega posterior, 09/12/2024** — o dia de pico, 215 paradas, para mostrar
   que escala.
5. Abrir um romaneio PDF e o link do Google Maps.

## Limitações conhecidas

- Geocodificação automática acerta 70–85%; o resto entra na fila de revisão manual.
- O baseline de locação é aproximado (ordem de lançamento), e isso é sinalizado na UI.
- Nada é gravado no Firebird do cliente. Escrever a rota de volta em
  `ENTREGA_PCAB` é fase 2.
- Perfil de rota é `car`. Restrição de caminhão (altura/peso) exige trocar o
  OSRM por Valhalla.
````

- [ ] **Step 6: Commit**

```bash
git add tests/test_e2e.py scripts/seed_demo.py README.md
git commit -m "test: verificacao ponta a ponta dos criterios de sucesso e preparo da demo"
```

---

## Ordem de execução e paralelismo

`prepare_osm.sh` (Task 1, Step 8) e a construção do índice de ruas (Task 5, Step 6) são as
duas etapas longas. Disparar ambas em background assim que possível:

```
Task 1  ──> dispara prepare_osm.sh ──────────────┐ (20-40 min)
Tasks 2,3,4  (config, firebird, normalize)       │  não dependem do mapa
Task 5  ──> dispara índice de ruas ──────────────┤  depende do .pbf
Tasks 7,8  (StopSources)                         │  não dependem do mapa
Task 6  (geocoder)                        <──────┘  depende do índice
Tasks 9,10,11  (osrm, vroom, optimizer)   <─────────depende do OSRM pronto
Tasks 12,13,14,15
```

Tasks 2, 3, 4, 7 e 8 são testáveis sem OSRM. Tasks 9–15 exigem a stack no ar.

