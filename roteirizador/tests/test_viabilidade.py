# tests/test_viabilidade.py
"""A regra de governo do projeto, testada isolada do ERP e do OSRM:
*uma viagem de baseline que não poderia ter sido executada nunca pode ser
apresentada como real*.

Cobre a física de carga hoistada para `erp/base.py` (que estava presa dentro
de `LocacaoSource` e por isso não protegia o caminho de entrega posterior),
a distinção entre caminhão e caixa de despacho do ERP, e o portão de
viabilidade de `service.viagens_inviaveis`.
"""
import pytest

from api.app.erp.base import (cabe, placa_de_veiculo_real,
                              quebrar_por_capacidade)
from api.app.erp.entrega_posterior import EntregaPosteriorSource
from api.app.models import Address, BaselineTrip, Stop, VehicleConfig
from api.app.service import viagens_inviaveis

_END = Address(None, None, None, "DOURADOS", "MS", None, "")


def _stop(ext, kind="delivery", seq=0, amount=1):
    return Stop(external_id=ext, kind=kind, cliente_id=1, cliente_nome="C",
                address=_END, amount=amount, erp_sequence=seq)


# ---- física de carga (hoistada de LocacaoSource) --------------------------
def test_cabe_e_a_mesma_fisica_nos_dois_perfis():
    entrega, outra, coleta = (_stop("a"), _stop("b"),
                              _stop("c", kind="pickup"))
    assert cabe([entrega, coleta], 1)          # sai cheio, volta cheio
    assert not cabe([entrega, outra], 1)       # duas caçambas a bordo
    assert not cabe([coleta, entrega], 1)      # coleta antes: não sobra espaço
    assert cabe([coleta, entrega], 2)


def test_quebrar_por_capacidade_nao_reordena_e_respeita_o_limite():
    seq = [_stop(f"e{i}", seq=i) for i in range(5)]
    viagens = quebrar_por_capacidade(seq, 2)
    assert [[s.external_id for s in v] for v in viagens] == [
        ["e0", "e1"], ["e2", "e3"], ["e4"]]
    assert all(cabe(v, 2) for v in viagens)
    # a ordem registrada é preservada: nada é saltado nem trocado de lugar
    assert [s.external_id for v in viagens for s in v] == [s.external_id
                                                           for s in seq]


def test_quebrar_por_capacidade_isola_parada_maior_que_o_caminhao():
    """Uma parada que sozinha estoura a capacidade não pode travar o laço
    nem ser silenciosamente juntada a outra: vira viagem própria, e continua
    inviável para o portão reclamar."""
    viagens = quebrar_por_capacidade(
        [_stop("a"), _stop("gigante", amount=9), _stop("b")], 2)
    assert [[s.external_id for s in v] for v in viagens] == [
        ["a"], ["gigante"], ["b"]]


# ---- caminhão de verdade x caixa de despacho ------------------------------
@pytest.mark.parametrize("placa,esperado", [
    ("AEY 2862", True), ("HQY-8879", True), ("HRY0575", True),
    ("OOP-912", True), ("RJR3J05", True),
    ("DV", False), ("RETIRA", False), ("BAIXA DV", False), ("CIF", False),
    ("MADEGRAN", False), ("PRIMAVER", False), ("01", False), ("1H", False),
    ("508", False), ("TMP01", False), (None, False), ("", False),
])
def test_so_e_veiculo_o_que_tem_placa_de_veiculo(placa, esperado):
    """As linhas da tabela VEICULO do ERP são metade frota e metade caixa de
    despacho. Confundir as duas é o que fez 125 paradas de "ENTREGA
    DUVIDOSA / BAIXA DV" virarem uma rota de caminhão."""
    assert placa_de_veiculo_real(placa) is esperado


# ---- baseline de entrega posterior ----------------------------------------
def _veiculo(cap=12, trips=1, turno_h=11):
    return VehicleConfig(id="V", label="V", capacity=cap, trips=trips,
                         shift_start_s=7 * 3600,
                         shift_end_s=(7 + turno_h) * 3600)


class _Frota(list):
    """Lista de `Vehicle`-like: `baseline_order` só lê `.capacity`."""


class _V:
    def __init__(self, capacity):
        self.capacity = capacity


def test_baseline_registrado_e_factivel_nao_e_aproximado():
    src = EntregaPosteriorSource(conn=None)
    stops = [_stop(f"EP:{i}", seq=i) for i in range(4)]
    for s in stops:
        s.erp_vehicle_id = 33
    trips = src.baseline_order(stops, [_V(12)])
    assert len(trips) == 1
    assert src.baseline_approximate is False
    assert src.baseline_note == ""


def test_baseline_sem_veiculo_registrado_se_declara_aproximado():
    """As paradas que o ERP jogou numa caixa de despacho chegam sem veículo:
    a viagem delas é RECONSTRUÍDA, e dizer isso é a diferença entre um
    número honesto e os 77% do dia de pico."""
    src = EntregaPosteriorSource(conn=None)
    stops = [_stop(f"EP:{i}", seq=i) for i in range(3)]
    stops[0].erp_vehicle_id = 33
    trips = src.baseline_order(stops, [_V(12)])
    assert src.baseline_approximate is True
    assert "não têm caminhão registrado" in src.baseline_note
    assert sum(len(t.stop_external_ids) for t in trips) == 3


def test_baseline_quebra_grupo_grande_demais_e_avisa():
    src = EntregaPosteriorSource(conn=None)
    stops = [_stop(f"EP:{i}", seq=i) for i in range(30)]
    for s in stops:
        s.erp_vehicle_id = 33
    trips = src.baseline_order(stops, [_V(12)])
    assert len(trips) == 3                      # 12 + 12 + 6
    assert src.baseline_approximate is True
    assert "quebrados pela capacidade" in src.baseline_note
    assert all(cabe([s for s in stops if s.external_id in t.stop_external_ids],
                    12) for t in trips)


# ---- o portão --------------------------------------------------------------
def test_portao_pega_viagem_que_estoura_a_capacidade():
    stops = [_stop(f"e{i}", seq=i) for i in range(20)]
    trip = BaselineTrip("Veículo 11", [s.external_id for s in stops])
    assert viagens_inviaveis([trip], stops, [_veiculo(cap=12)])


def test_portao_pega_viagem_que_nao_cabe_no_turno():
    stops = [_stop("e0")]
    trip = BaselineTrip("Veículo 11", ["e0"])
    trip.duration_s = int(31.6 * 3600)          # o dia de pico, medido
    motivos = viagens_inviaveis([trip], stops, [_veiculo(turno_h=11)])
    assert motivos and "turno" in motivos[0]


def test_portao_silencia_quando_tudo_e_executavel():
    stops = [_stop(f"e{i}", seq=i) for i in range(5)]
    trip = BaselineTrip("Veículo 33", [s.external_id for s in stops])
    trip.duration_s = 6 * 3600
    assert viagens_inviaveis([trip], stops, [_veiculo(cap=12)]) == []


def test_portao_usa_o_MAIOR_veiculo_e_o_MAIOR_turno_da_frota():
    """Se nem o melhor caso da frota executaria a viagem, ela não é real --
    mas basta UM veículo dar conta para ela ser possível. O portão é a rede
    de segurança, não um segundo otimizador."""
    stops = [_stop(f"e{i}", seq=i) for i in range(10)]
    trip = BaselineTrip("X", [s.external_id for s in stops])
    trip.duration_s = 10 * 3600
    frota = [_veiculo(cap=2, turno_h=4), _veiculo(cap=12, turno_h=11)]
    assert viagens_inviaveis([trip], stops, frota) == []
    assert viagens_inviaveis([trip], stops, [frota[0]])
