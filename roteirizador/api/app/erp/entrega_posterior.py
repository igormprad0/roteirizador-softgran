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
