from __future__ import annotations
from datetime import date

from ..config import ImportMode, Profile
from ..db.firebird import ErpConnection
from ..models import Address, BaselineTrip, Stop, Vehicle
from .base import placa_de_veiculo_real, quebrar_por_capacidade

_SQL = """
SELECT p.ID_CONTROLE AS PCAB_ID, p.ID_ENTREGA, p.DATA, p.HORA, p.ID_VEICULO,
       p.FLAG_DEV_VENDAS,
       e.ID_CLIENTE, e.CLIENTE_NOME, e.LOGRADOURO, e.CLIENTE_ENDERECO,
       e.CLIENTE_BAIRRO, e.CLIENTE_CIDADE, e.CLIENTE_UF, e.VENCIMENTO,
       e.DOCUMENTO, e.TIPO_ENTREGA,
       cf.NOME AS CF_NOME, cf.ENDERECO AS CF_ENDERECO, cf.NUMERO AS CF_NUMERO,
       cf.BAIRRO AS CF_BAIRRO, cf.CIDADE AS CF_CIDADE, cf.UF AS CF_UF, cf.CEP AS CF_CEP,
       v.PLACA AS VEICULO_PLACA, v.DESCRICAO AS VEICULO_DESCRICAO
FROM ENTREGA_PCAB p
JOIN ENTREGA_CAB e ON e.ID_CONTROLE = p.ID_ENTREGA
LEFT JOIN CLIFOR cf ON cf.ID = e.ID_CLIENTE
LEFT JOIN VEICULO v ON v.ID_VEICULO = p.ID_VEICULO
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
        # Preenchidos por `baseline_order` -- ver o docstring dele.
        self.baseline_approximate = False
        self.baseline_note = ""

    def fetch(self, target_date: date, mode: ImportMode) -> list[Stop]:
        rows = self._conn.query(_SQL.format(situacao=_SITUACAO[mode]), (target_date,))
        stops: list[Stop] = []
        for seq, r in enumerate(rows):
            # O número só vale se vier do MESMO cadastro que o logradouro.
            # `CF_NUMERO` é o número do endereço de CADASTRO do cliente; se o
            # logradouro veio do endereço de ENTREGA (que já traz o número
            # dentro do texto), colar o do cadastro em cima é trocar a porta:
            # `normalize.py` dá precedência a `addr.numero` sobre o número
            # extraído do texto, então "LANNINO QUADRA 10 LOTE 07, 345"
            # virava número 50 (do cadastro "JATOBA, 50"). Mesma regra que
            # `locacao.py::_address` já aplicava.
            entrega = _s(r["LOGRADOURO"]) or _s(r["CLIENTE_ENDERECO"])
            logradouro = entrega or _s(r["CF_ENDERECO"])
            addr = Address(
                logradouro=logradouro,
                numero=None if entrega else _s(r["CF_NUMERO"]),
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
                # Só conta como veículo o que tem PLACA de veículo. As
                # outras linhas da tabela VEICULO são caixas de despacho
                # ("DEVOLUÇÃO/DV", "PRÓPRIO/RETIRA", "ENTREGA DUVIDOSA/
                # BAIXA DV", "DEP TRANSMITO/CIF") -- nenhum caminhão saiu
                # com elas, então a parada fica sem veículo registrado e o
                # baseline dela é RECONSTRUÍDO, não medido (ver
                # `baseline_order`).
                erp_vehicle_id=(int(r["ID_VEICULO"])
                                if r["ID_VEICULO"] is not None
                                and placa_de_veiculo_real(_s(r["VEICULO_PLACA"]))
                                else None),
                erp_sequence=seq,
            ))
        return stops

    def baseline_order(self, stops: list[Stop],
                       vehicles: list[Vehicle]) -> list[BaselineTrip]:
        """A rota que a operação executou: agrupada pelo veículo do ERP, na
        ordem de HORA (já refletida em erp_sequence pelo ORDER BY do fetch).

        NÃO é factível por construção, ao contrário do que esta função
        presumiu até aqui. Duas coisas quebram a premissa e as duas
        aparecem nos dados reais:

        1. Boa parte de `ID_VEICULO` não é caminhão nenhum, é caixa de
           despacho (ver `fetch`). No dia de pico, 125 das 128 paradas
           atendidas estavam sob "ENTREGA DUVIDOSA / BAIXA DV" -- e o
           agrupamento antigo as roteava como UMA volta contínua de 391 km
           e 31,6 h, reportada com `approximate=False`. Nenhum caminhão fez
           aquilo. Essas paradas chegam aqui sem veículo e o baseline delas
           é reconstruído.
        2. Mesmo um caminhão de verdade não carrega o dia inteiro de uma
           vez: ele volta ao depósito para recarregar, e o ERP não registra
           onde. Cada grupo é quebrado em viagens consecutivas que cabem na
           maior capacidade configurada (`vehicles`), SEM reordenar nada --
           a ordem continua sendo a registrada.

        Quando qualquer uma das duas dispara, `baseline_approximate` fica
        True e a ressalva vai para a UI. Um baseline que não poderia ter
        sido executado nunca pode ser apresentado como real.
        """
        self.baseline_approximate = False
        self.baseline_note = ""
        if not stops:
            return []

        capacidade = max((v.capacity for v in vehicles), default=0)
        grupos: dict[int | None, list[Stop]] = {}
        for s in sorted(stops, key=lambda x: x.erp_sequence):
            grupos.setdefault(s.erp_vehicle_id, []).append(s)

        sem_veiculo = len(grupos.get(None, []))
        quebrados = 0
        trips: list[BaselineTrip] = []
        for vid, grupo in sorted(grupos.items(),
                                 key=lambda kv: (kv[0] is None, kv[0])):
            label = f"Veículo {vid}" if vid else "Sem veículo registrado"
            partes = ([grupo] if capacidade <= 0
                      else quebrar_por_capacidade(grupo, capacidade))
            if len(partes) > 1:
                quebrados += 1
            for i, parte in enumerate(partes, start=1):
                trips.append(BaselineTrip(
                    label=label if len(partes) == 1 else f"{label} — viagem {i}",
                    stop_external_ids=[s.external_id for s in parte]))

        motivos = []
        if sem_veiculo:
            motivos.append(
                f"{sem_veiculo} de {len(stops)} paradas não têm caminhão "
                f"registrado no ERP (caem em caixas de despacho como "
                f"DV/RETIRA/BAIXA DV/CIF); a viagem delas foi reconstruída")
        if quebrados:
            motivos.append(
                f"{quebrados} grupo(s) do ERP não caberiam em uma única "
                f"viagem e foram quebrados pela capacidade configurada")
        if motivos:
            self.baseline_approximate = True
            self.baseline_note = "baseline parcialmente reconstruído: " + \
                "; ".join(motivos) + "."
        return trips
