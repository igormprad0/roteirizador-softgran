from __future__ import annotations
from datetime import date, timedelta

from ..config import ImportMode, Profile
from ..db.firebird import ErpConnection
from ..models import Address, BaselineTrip, Stop, Vehicle
from .base import cabe

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


APROX_LOCACAO = ("baseline aproximado: o ERP não registra veículo nem ordem "
                 "para locação; usada a ordem de lançamento")


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
        # O ERP de locação não registra veículo nem ordem de rota: este
        # baseline é SEMPRE uma reconstrução, e diz isso de si mesmo em vez
        # de `service.py` deduzir pelo perfil.
        self.baseline_approximate = True
        self.baseline_note = APROX_LOCACAO

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

    def baseline_order(self, stops: list[Stop],
                       vehicles: list[Vehicle]) -> list[BaselineTrip]:
        """Aproximado (§5.4 do spec): o ERP não registra veículo nem ordem para
        locação. Reproduz um operador despachando a lista de lançamento de
        cima para baixo para a próxima viagem disponível -- mas cada viagem
        respeita a MESMA capacidade que o otimizador usa (`vehicles`, a
        lista expandida de `routing/vroom.py::expand_trips`, o mesmo objeto
        que o VROOM recebe).

        Antes desta versão, o baseline agrupava em blocos fixos de ~12
        paradas ignorando capacidade -- presumindo um caminhão saindo do
        depósito com doze caçambas. Uma poliguindaste carrega UMA. Isso
        comparava uma rota otimizada que obedece capacidade contra um
        baseline que a ignora: o otimizado era forçado a idas-e-vindas
        curtas (caro por natureza) contra um baseline que encadeava doze
        paradas sem nunca voltar ao depósito -- entendia menos economia do
        que existe de verdade, do mesmo jeito (e no mesmo tamanho de erro)
        que uma versão ainda mais antiga tinha inflado a economia ao
        comparar lados com números de parada diferentes (ver `service.py`).

        Uma entrega é pré-carregada no depósito (precisa estar a bordo antes
        de sair) e libera espaço ao ser entregue; uma coleta ocupa espaço ao
        ser recolhida -- por isso uma viagem de capacidade 1 PODE, sim,
        entregar um equipamento e na sequência coletar o vencido ("sai
        cheio, volta cheio"), mas não pode carregar duas entregas ao mesmo
        tempo. `erp/base.py::cabe` simula exatamente essa física -- mora lá,
        e não aqui, porque é física de carga e não regra de locação: a
        ausência dela no caminho de entrega posterior foi o que deixou uma
        volta contínua de 125 paradas passar por rota real."""
        if not stops or not vehicles:
            return []
        fila = sorted(stops, key=lambda s: s.erp_sequence)
        trips: list[BaselineTrip] = []
        for v in vehicles:
            if not fila:
                break
            viagem: list[Stop] = []
            # Varre a fila PARA A FRENTE em vez de exigir que a próxima
            # parada caiba na hora: um despachante que já tem a caçamba a
            # bordo pula o pedido que não cabe e leva o próximo que cabe --
            # ninguém manda o caminhão embora meio vazio porque a linha
            # seguinte da lista não coube. A versão anterior encerrava a
            # viagem no primeiro "não cabe" adjacente, o que com capacidade
            # 1 travava o baseline em `nº de viagens + 1` paradas
            # independentemente dos dados: o tamanho da frota disfarçado de
            # medição. A ordem de lançamento continua sendo respeitada (nada
            # é reordenado, só saltado) e a capacidade também.
            i = 0
            while i < len(fila):
                if cabe(viagem + [fila[i]], v.capacity):
                    viagem.append(fila.pop(i))
                else:
                    i += 1
            if viagem:
                # `v.label` já vem qualificado com "— viagem N" para N > 1
                # (montado em `expand_trips`); não duplicar aqui.
                trips.append(BaselineTrip(
                    label=v.label, stop_external_ids=[s.external_id for s in viagem]))
        return trips
