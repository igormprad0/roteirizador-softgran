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
from .routing.osrm import OsrmError

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
    except OsrmError as exc:
        # Optimizer.solve()/measure_baseline() deliberadamente deixam subir uma
        # rota que o OSRM não conseguiu medir, em vez de contá-la como 0 km (o
        # que inflaria a economia reportada — o pior sentido de errar num
        # número que existe para convencer um cliente cético). Aqui isso vira
        # um erro limpo para a UI mostrar, nunca um 500 com stack trace nem,
        # pior, uma resposta 200 com comparativo fabricado.
        raise HTTPException(502, f"falha ao medir rota no OSRM: {exc}") from exc


def _load_run(run_id: int) -> dict:
    run = service.get_run(run_id)
    if run is None:
        raise HTTPException(404, "execução não encontrada")
    return run


@app.get("/api/runs/{run_id}")
def get_run(run_id: int) -> dict:
    return _load_run(run_id)


def _rebuild(run: dict) -> tuple[list[Stop], list[VehicleRoute], Depot]:
    stops = [Stop(external_id=s["external_id"], kind=s["kind"],
                  cliente_id=s["cliente_id"], cliente_nome=s["cliente_nome"],
                  # Remonta o endereço ESTRUTURADO (ver service.stop_payload):
                  # colapsar tudo em `logradouro` deixava as colunas
                  # `bairro` e `cidade` do CSV vazias em toda exportação.
                  # `.get` porque execuções salvas antes desta versão não
                  # têm os campos novos no payload_json.
                  address=Address(s.get("logradouro") or s["address"],
                                  None, s.get("bairro"), s.get("cidade"),
                                  s.get("uf"), None, s["address"]),
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
