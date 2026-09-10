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
