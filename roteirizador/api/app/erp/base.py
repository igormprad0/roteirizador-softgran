from __future__ import annotations
import re
from datetime import date
from typing import Protocol

from ..config import ImportMode, Profile
from ..models import BaselineTrip, Stop, Vehicle


class StopSource(Protocol):
    profile: Profile
    # Preenchidos por `baseline_order`: um baseline só pode ser apresentado
    # como REAL quando cada viagem dele é a que o ERP de fato registrou. Se a
    # fonte precisou reconstruir qualquer coisa (ordem, veículo, quebra por
    # capacidade), ela diz aqui — e `service.optimize` propaga para
    # `comparison.approximate`, em vez de decidir por perfil no chute.
    baseline_approximate: bool
    baseline_note: str

    def fetch(self, target_date: date, mode: ImportMode) -> list[Stop]: ...

    def baseline_order(self, stops: list[Stop],
                       vehicles: list[Vehicle]) -> list[BaselineTrip]: ...


# ---------------------------------------------------------------- física
def cabe(viagem: list[Stop], capacidade: int) -> bool:
    """Simula a carga ao longo de UMA viagem, na ordem dada. Toda entrega da
    viagem precisa estar pré-carregada no depósito antes da partida (soma de
    todas as entregas, não só a próxima) -- por isso a carga inicial já conta
    como um pico a respeitar, não só os picos depois de cada coleta. Uma
    coleta ocupa espaço ao ser recolhida; uma entrega libera ao ser entregue,
    e é por isso que um poliguindaste de capacidade 1 pode entregar e depois
    coletar na mesma viagem ("sai cheio, volta cheio").

    Isto é física de carga, não regra de locação: mora aqui, e não dentro de
    `LocacaoSource`, porque a ausência dela no caminho de entrega posterior
    foi exatamente o defeito que deixou 125 paradas numa única volta contínua
    de 31,6 h serem reportadas como rota real (ver `entrega_posterior.py`).
    """
    carga = sum(s.amount for s in viagem if s.kind == "delivery")
    if carga > capacidade:
        return False
    for s in viagem:
        carga += -s.amount if s.kind == "delivery" else s.amount
        if carga > capacidade:
            return False
    return True


def quebrar_por_capacidade(stops: list[Stop], capacidade: int) -> list[list[Stop]]:
    """Quebra uma sequência JÁ ORDENADA em viagens consecutivas que cabem na
    capacidade, sem reordenar nada. Para um baseline registrado (entrega
    posterior) a ordem é o próprio registro -- o que se reconstrói é só onde
    o caminhão teve de voltar ao depósito para recarregar, que o ERP não
    guarda. Uma parada que sozinha estoura a capacidade vira uma viagem só
    dela (segue inviável, e o portão de viabilidade em `service.optimize`
    marca o baseline como aproximado por causa dela)."""
    viagens: list[list[Stop]] = []
    atual: list[Stop] = []
    for s in stops:
        if atual and not cabe(atual + [s], capacidade):
            viagens.append(atual)
            atual = []
        atual.append(s)
    if atual:
        viagens.append(atual)
    return viagens


# ---------------------------------------------------------------- veículos
# Uma PLACA de verdade tem pelo menos 3 letras e 3 dígitos (AAA1234 antigo,
# AAA1A11 Mercosul, e as truncadas do cadastro tipo "OOP-912"). O que não
# passa disto não é caminhão: é caixa de despacho do ERP -- "DV"
# (devolução), "RETIRA" (o cliente buscou), "BAIXA DV" (entrega duvidosa,
# baixa administrativa), "CIF" (frete de terceiro), nomes de depósito. Essas
# linhas nunca foram uma rota: agrupá-las como se fossem um caminhão foi o
# que produziu uma "volta contínua" de 125 paradas / 31,6 h no dia de pico e
# a reportou como baseline real.
_PLACA_SUJEIRA = re.compile(r"[^A-Z0-9]")


def placa_de_veiculo_real(placa: str | None) -> bool:
    if not placa:
        return False
    p = _PLACA_SUJEIRA.sub("", placa.upper())
    if len(p) < 6:
        return False
    letras = sum(c.isalpha() for c in p)
    digitos = sum(c.isdigit() for c in p)
    return letras >= 3 and digitos >= 3


def build_source(profile: Profile, conn) -> StopSource:
    from .entrega_posterior import EntregaPosteriorSource
    from .locacao import LocacaoSource

    if profile is Profile.LOCACAO:
        return LocacaoSource(conn)
    if profile is Profile.ENTREGA_POSTERIOR:
        return EntregaPosteriorSource(conn)
    raise ValueError(f"perfil sem StopSource: {profile}")
