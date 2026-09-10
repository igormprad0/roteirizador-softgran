from __future__ import annotations

from ..erp.base import cabe
from ..models import (BaselineResult, BaselineTrip, Comparison, Depot,
                      Solution, Stop)

# Contra QUAL ordenação o baseline foi medido. Ver `measure_baseline`.
REGISTRADA = "registrada"
VIZINHO_MAIS_PROXIMO = "vizinho_mais_proximo"


def _ordem_vizinho_mais_proximo(seq: list[Stop], depot: Depot,
                                capacidade: int) -> list[Stop] | None:
    """As MESMAS paradas da viagem, na ordem de um despachante competente:
    a cada passo, a parada não visitada mais próxima que ainda respeita a
    capacidade. A escolha usa distância em linha reta de propósito -- uma
    matriz OSRM por viagem custaria caro para uma heurística, e o resultado
    é medido no OSRM de verdade logo depois. Devolve None quando o guloso
    não fecha a viagem (a física de carga pode travar: com capacidade 1,
    coletar antes de entregar não cabe) -- aí vale a ordem registrada."""
    restantes, ordem = list(seq), []
    lon, lat = depot.coord
    while restantes:
        cabem = [s for s in restantes if cabe(ordem + [s], capacidade)]
        if not cabem:
            return None
        prox = min(cabem, key=lambda s: (s.geo.lon - lon) ** 2
                   + (s.geo.lat - lat) ** 2)
        ordem.append(prox)
        restantes.remove(prox)
        lon, lat = prox.geo.coord
    return ordem


def measure_baseline(trips: list[BaselineTrip], stops: list[Stop], osrm,
                     depot: Depot, approximate: bool = False,
                     note: str = "",
                     capacidade: int | None = None) -> BaselineResult:
    """Mede cada viagem suposta contra o OSRM, fechando o circuito no
    depósito e somando o tempo de serviço.

    Com `capacidade` informada, cada viagem é medida em DUAS ordens -- a
    registrada/de lançamento e a de vizinho mais próximo das mesmas paradas
    -- e fica com a MENOR. A composição das viagens não muda: só a ordem
    dentro de cada uma.

    Isto existe porque a ordem registrada não carrega informação espacial
    nenhuma: 10 embaralhamentos aleatórios das paradas de cada dia medem o
    mesmo que ela (pico 401,5 km contra 407,4 de média aleatória; 13/08
    278,6 contra 283,3; locação 119,0 contra 121,8). Comparar o otimizador
    contra um gerador de números aleatórios não é comparação -- o
    despachante do cliente dirige para a parada mais próxima. Uma economia
    que só sobrevive contra a ordem de digitação do ERP não é economia.
    Custa uma chamada `route` extra por viagem, e nenhuma matriz."""
    por_id = {s.external_id: s for s in stops}
    dist = dur = 0
    usados = 0
    km_por_metodo = {REGISTRADA: 0, VIZINHO_MAIS_PROXIMO: 0}

    for trip in trips:
        seq = [por_id[i] for i in trip.stop_external_ids
               if i in por_id and por_id[i].geo is not None
               and por_id[i].geo.confidence != "failed"]
        if not seq:
            continue
        usados += 1
        leg = osrm.route([depot.coord] + [s.geo.coord for s in seq]
                         + [depot.coord])
        metodo = REGISTRADA
        alt = (_ordem_vizinho_mais_proximo(seq, depot, capacidade)
               if capacidade is not None else None)
        if alt is not None and [s.external_id for s in alt] != [
                s.external_id for s in seq]:
            leg_alt = osrm.route([depot.coord] + [s.geo.coord for s in alt]
                                 + [depot.coord])
            if leg_alt.distance_m < leg.distance_m:
                leg, seq, metodo = leg_alt, alt, VIZINHO_MAIS_PROXIMO
                # A viagem passa a SER a ordem medida -- quem lê
                # `stop_external_ids` (payload, teste e2e de viabilidade)
                # precisa ver a mesma sequência que foi cronometrada. Ids
                # sem geocodificação ficaram fora de `seq` e voltam ao fim,
                # na ordem em que estavam.
                novos = [s.external_id for s in alt]
                trip.stop_external_ids = novos + [
                    i for i in trip.stop_external_ids if i not in set(novos)]
        trip.method = metodo
        km_por_metodo[metodo] += leg.distance_m
        trip.distance_m = leg.distance_m
        trip.duration_s = leg.duration_s + sum(s.service_seconds for s in seq)
        dist += trip.distance_m
        dur += trip.duration_s

    # Qual ordenação responde pela maior parte da quilometragem medida. A
    # escolha é por viagem; este é o rótulo do conjunto, para a UI e o
    # README dizerem contra o que o número foi medido. Empate (inclusive
    # zero) fica em "registrada" -- o rótulo mais conservador.
    metodo = (VIZINHO_MAIS_PROXIMO
              if km_por_metodo[VIZINHO_MAIS_PROXIMO] > km_por_metodo[REGISTRADA]
              else REGISTRADA)
    return BaselineResult(trips=trips, total_distance_m=dist, total_duration_s=dur,
                          vehicles_used=usados, approximate=approximate, note=note,
                          method=metodo)


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
        baseline_method=baseline.method,
    )
