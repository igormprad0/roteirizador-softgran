from __future__ import annotations
import dataclasses
from datetime import date

from .config import ImportMode, PROFILES, Profile, get_settings
from .db.firebird import connect
from .db.local import LocalStore
from .erp.base import build_source, cabe
from .export.maps_link import waze_link
from .geo.geocoder import Geocoder
from .models import BaselineTrip, Depot, Stop, VehicleConfig, VehicleRoute
from .routing.baseline import compare, measure_baseline
from .routing.optimizer import Optimizer
from .routing.osrm import OsrmClient
from .routing.vroom import expand_trips


def viagens_inviaveis(trips: list[BaselineTrip], stops: list[Stop],
                      fleet: list[VehicleConfig]) -> list[str]:
    """Portão de viabilidade do baseline, aplicado a TODO perfil.

    Regra de governo deste projeto: *uma viagem de baseline que não poderia
    ter sido executada nunca pode ser apresentada como real*. Cinco vezes
    neste projeto um defeito fez o número do painel melhorar sem quebrar
    teste nenhum, e o caso mais caro foi este: 125 paradas de uma caixa de
    despacho do ERP roteadas como uma volta contínua de 391 km e 31,6 h,
    comparadas contra 89,8 km otimizados, resultando em "77% de economia,
    approximate=False". Nenhum caminhão poderia ter feito aquela volta.

    Este portão não conserta a reconstrução do baseline (isso é papel de
    cada `StopSource.baseline_order`) -- ele é a rede que pega o que passar,
    incluindo casos futuros que ninguém previu. Compara cada viagem contra o
    MAIOR veículo e o MAIOR turno configurados: se nem o melhor caso da
    frota executaria a viagem, ela não é real.

    Devolve a lista de motivos (vazia = tudo factível)."""
    if not fleet or not trips:
        return []
    cap_max = max(v.capacity for v in fleet)
    turno_max = max(v.shift_end_s - v.shift_start_s for v in fleet)
    por_id = {s.external_id: s for s in stops}
    excesso_carga: list[str] = []
    excesso_turno: list[str] = []
    for t in trips:
        seq = [por_id[i] for i in t.stop_external_ids if i in por_id]
        if seq and not cabe(seq, cap_max):
            excesso_carga.append(f"{t.label} ({len(seq)} paradas)")
        if t.duration_s > turno_max:
            excesso_turno.append(f"{t.label} ({t.duration_s / 3600:.1f} h)")

    motivos = []
    if excesso_carga:
        motivos.append(
            f"{len(excesso_carga)} viagem(ns) do baseline excedem a maior "
            f"capacidade da frota ({cap_max}): {', '.join(excesso_carga[:3])}"
            + (" ..." if len(excesso_carga) > 3 else ""))
    if excesso_turno:
        motivos.append(
            f"{len(excesso_turno)} viagem(ns) do baseline não caberiam no "
            f"maior turno configurado ({turno_max / 3600:.0f} h): "
            f"{', '.join(excesso_turno[:3])}"
            + (" ..." if len(excesso_turno) > 3 else ""))
    return motivos


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
        # Os campos estruturados vão junto do texto colapsado: o CSV tem
        # colunas `bairro` e `cidade` e `main._rebuild` remonta as paradas a
        # partir DESTE payload -- sem isso as duas colunas saíam sempre
        # vazias, porque só sobrevivia a string concatenada.
        "logradouro": a.logradouro, "bairro": a.bairro, "cidade": a.cidade,
        "uf": a.uf,
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
        # Deep link de navegação por parada (§6.3 do spec). Vai no payload,
        # e não montado no cliente, para o app.js e o romaneio PDF usarem
        # exatamente a mesma função -- inversão de lat/lon é o erro clássico
        # aqui e não pode existir em duas versões.
        "steps": [{"seq": s.seq, "stop_external_id": s.stop_external_id,
                   "kind": s.kind, "lon": s.lon, "lat": s.lat,
                   "arrival_s": s.arrival_s, "load_after": s.load_after,
                   "waze_url": waze_link(s)}
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

    # O comparativo só é honesto se os dois lados medirem o mesmo trabalho.
    # A frota configurada pode não caber todas as paradas do dia (VROOM
    # devolve `unassigned` para o que não coube) — medir o baseline contra
    # TODAS as paradas enquanto o otimizado só serve um subconjunto infla a
    # economia com o trabalho que simplesmente não foi feito, não com
    # roteirização melhor. Restringe o baseline às paradas que a solução
    # de fato atendeu.
    atendidas_ids = {s.stop_external_id for r in solution.routes for s in r.steps}
    atendidas = [s for s in stops if s.external_id in atendidas_ids]

    # Mesma frota expandida (mesmas viagens, mesma capacidade por viagem)
    # que o otimizador usa -- o baseline de locação precisa respeitar a
    # mesma física de capacidade que a rota otimizada é obrigada a
    # respeitar, senão o comparativo mede um lado contra uma rota que não
    # poderia ter sido executada (ver `erp/locacao.py::baseline_order`).
    with connect(profile) as conn:
        source = build_source(profile, conn)
        trips = source.baseline_order(atendidas, expand_trips(fleet, depot))
        # Quem sabe se o baseline é medido ou reconstruído é a fonte, não
        # este arquivo. Antes daqui a regra era `profile is Profile.LOCACAO`
        # -- entrega posterior saía com `approximate=False` incondicional,
        # inclusive no dia em que 125 das 128 paradas vinham de uma caixa de
        # despacho que nunca foi caminhão nenhum.
        aproximado = source.baseline_approximate
        note = source.baseline_note
    osrm = OsrmClient(get_settings().osrm_url)
    base = measure_baseline(trips, atendidas, osrm, depot,
                            approximate=aproximado, note=note)

    # Portão de viabilidade: vale para todo perfil e roda DEPOIS da medição,
    # porque só aí cada viagem tem duração. Ver `viagens_inviaveis`.
    inviaveis = viagens_inviaveis(trips, atendidas, fleet)
    if inviaveis:
        aproximado = True
        note = ((note + " " if note else "")
                + "baseline NÃO executável: " + "; ".join(inviaveis)
                + ". A comparação abaixo é contra uma rota que a frota "
                  "configurada não conseguiria fazer -- trate como indicativo.")
        base = dataclasses.replace(base, approximate=True, note=note)

    # O baseline aproximado respeita ordem de lançamento E capacidade (ver
    # `LocacaoSource.baseline_order`) -- diferente do otimizador, ele não
    # pode reordenar paradas para encaixar mais gente na mesma frota. Pode,
    # portanto, deixar de colocar em ALGUMA viagem parte das paradas que o
    # otimizador atendeu (ele reordena; o baseline "aproximado" não pode,
    # por definição). Comparar um baseline sobre menos paradas contra um
    # otimizado sobre mais paradas reintroduz o mesmo tipo de injustiça que
    # a Task 13 corrigiu (medir lados com quantidades de trabalho
    # diferentes) -- só que no sentido oposto: baseline artificialmente
    # barato, "economia" artificialmente negativa. Quando isso acontece,
    # restringe os dois lados às paradas que o baseline também conseguiu
    # colocar em alguma viagem, medindo o lado otimizado do mesmo jeito que
    # o baseline (uma "viagem" OSRM por rota, na sequência que o VROOM já
    # decidiu -- não uma reotimização).
    cobertas_pelo_baseline = {i for t in trips for i in t.stop_external_ids}
    solucao_comparavel = solution
    if cobertas_pelo_baseline and cobertas_pelo_baseline != atendidas_ids:
        comparaveis = [s for s in atendidas if s.external_id in cobertas_pelo_baseline]
        trips_otimizado = [
            BaselineTrip(
                label=r.vehicle_id,
                stop_external_ids=[s.stop_external_id
                                   for s in sorted(r.steps, key=lambda x: x.seq)
                                   if s.stop_external_id in cobertas_pelo_baseline])
            for r in solution.routes
        ]
        trips_otimizado = [t for t in trips_otimizado if t.stop_external_ids]
        otimizado_restrito = measure_baseline(trips_otimizado, comparaveis, osrm, depot,
                                              approximate=False, note="")
        solucao_comparavel = dataclasses.replace(
            solution, total_distance_m=otimizado_restrito.total_distance_m,
            total_duration_s=otimizado_restrito.total_duration_s)
        base = dataclasses.replace(base, note=(
            f"{note} comparativo restrito a {len(comparaveis)} das "
            f"{len(atendidas)} paradas atendidas -- o baseline aproximado "
            f"não conseguiu encaixar o restante na mesma frota respeitando "
            f"a ordem de lançamento (o otimizador pode reordenar; este "
            f"baseline, por definição, não pode)."
        ).strip())

    comp = compare(solucao_comparavel, base, cost_per_km=cost_per_km)
    # Quantas paradas cada lado realmente cobre com a MESMA frota -- não só
    # o km/km_saved acima. Para locação, capacidade 1 deixa pouca margem de
    # ganho em distância (a rota já é quase determinada pela física); o
    # ganho real é cobertura: `optimized_stops` (o que o otimizador atende
    # de fato, sem a restrição de justiça do km) contra `baseline_stops` (o
    # que um despacho ingênuo, preservando ordem de lançamento sem poder
    # reordenar, consegue encaixar na mesma frota). Estruturado como número,
    # não só em `note`, para a UI poder mostrar sem parsear texto em
    # português.
    comp = dataclasses.replace(
        comp, baseline_stops=len(cobertas_pelo_baseline), optimized_stops=len(atendidas))

    payload = {
        "profile": profile.value,
        "mode": mode.value,
        "counts": counts,
        "depot": {"label": depot.label, "lon": depot.lon, "lat": depot.lat,
                  "address": depot.address},
        "stops": [stop_payload(s) for s in stops],
        # O baseline deixa de ser um número opaco: cada viagem que ele supõe
        # vai no payload, com o que foi medido nela. É contra isto que o
        # teste e2e verifica viabilidade por conta própria (capacidade e
        # turno) em vez de acreditar no veredito de `viagens_inviaveis` --
        # um portão que só se auto-confirma não protege ninguém.
        "baseline": {
            "approximate": base.approximate,
            "note": base.note,
            "infeasible": inviaveis,
            "trips": [{"label": t.label,
                       "stop_external_ids": list(t.stop_external_ids),
                       "distance_km": round(t.distance_m / 1000, 1),
                       "duration_h": round(t.duration_s / 3600, 2)}
                      for t in trips],
        },
        "routes": [route_payload(r) for r in solution.routes if r.steps],
        "unassigned": [{"stop_external_id": u.stop_external_id, "reason": u.reason}
                       for u in solution.unassigned],
        "totals": {"distance_km": round(solution.total_distance_m / 1000, 1),
                   "duration_h": round(solution.total_duration_s / 3600, 1),
                   "vehicles_used": len([r for r in solution.routes if r.steps]),
                   # A economia acima só vale para estas paradas — não para
                   # o dia inteiro. Quem lê o número precisa saber quanto
                   # ficou de fora.
                   "stops_total": len(stops),
                   "stops_served": len(atendidas),
                   "stops_unassigned": len(stops) - len(atendidas)},
        "comparison": comp.__dict__,
    }
    payload["run_id"] = st.save_run(profile, target_date.isoformat(), payload)
    return payload


def get_run(run_id: int) -> dict | None:
    """Dono canônico de "o que é o run_id de uma execução salva" -- em vez de
    remendar isso na borda HTTP (main.py). `LocalStore.save_run` grava o
    payload_json ANTES de o próprio id existir (só existe depois do INSERT em
    `optimize()` acima), então o JSON persistido nunca traz a chave
    "run_id" -- só o dict devolvido por `optimize()` a tem, por já estar em
    memória quando é atribuída. `LocalStore.get_run` devolve "id" (a mesma
    coisa, vinda da linha). Completar aqui, e não em cada chamador, evita
    remendo espalhado; um `db/local.py` com suporte a update seria a correção
    completa, mas está fora do escopo deste arquivo."""
    run = store().get_run(run_id)
    if run is not None:
        run.setdefault("run_id", run["id"])
    return run
