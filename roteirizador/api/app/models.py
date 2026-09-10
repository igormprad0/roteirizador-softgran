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
    # Preenchidos por `routing/baseline.py::measure_baseline`. Existem para
    # o portão de viabilidade em `service.optimize` poder perguntar "esta
    # viagem caberia num turno?" sem roteirizar tudo de novo.
    distance_m: int = 0
    duration_s: int = 0
    # Contra qual ordenação ESTA viagem foi medida: "registrada" ou
    # "vizinho_mais_proximo", a que saiu mais curta (ver `measure_baseline`).
    method: str = "registrada"


@dataclass
class BaselineResult:
    trips: list[BaselineTrip]
    total_distance_m: int
    total_duration_s: int
    vehicles_used: int
    approximate: bool                # True para locação (§5.4 do spec)
    note: str = ""
    # Ordenação que responde pela maior parte da quilometragem do baseline.
    method: str = "registrada"


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
    # Quantas paradas cada lado do comparativo cobre (nem sempre iguais --
    # ver `service.py::optimize`). Para locação, capacidade 1 deixa pouca
    # margem de ganho em km; o ganho real aparece aqui: quantas paradas a
    # MESMA frota consegue cobrir num dia, otimizada vs. despacho ingênuo
    # que preserva a ordem de lançamento sem poder reordenar.
    baseline_stops: int = 0
    optimized_stops: int = 0
    # Contra QUAL ordenação do mesmo conjunto de viagens a economia foi
    # medida. A ordem registrada pelo ERP não carrega informação espacial
    # (mede igual a embaralhar as paradas), então o baseline mede as duas e
    # fica com a mais curta por viagem; este campo diz qual dominou, para a
    # tela e o README não venderem economia contra um sorteio.
    baseline_method: str = "registrada"
