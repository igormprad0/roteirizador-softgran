"""O sistema tem de dizer o que está errado e o que fazer.

Motivação real: depois de recopiar os `.fdb` (o que apaga os privilégios do
usuário `rotas`), a tela devolvia "Falha ao importar" e a causa — uma
`DatabaseError: no permission for SELECT access to TABLE CLIFOR` — ficava
enterrada num HTTP 500. Quem instala numa máquina nova bate exatamente nisso
e não tem como adivinhar que a resposta é rodar `scripts/grant_rotas.sh`.
"""
import firebird.driver as fb
import pytest
from fastapi.testclient import TestClient

from api.app import service
from api.app.config import Profile
from api.app.main import app

client = TestClient(app)


# ---- a mensagem de remédio ------------------------------------------------

def test_erro_de_permissao_vira_remedio_acionavel():
    erro = fb.DatabaseError("no permission for SELECT access to TABLE CLIFOR")
    d = service.diagnosticar_erro_de_base(erro)
    assert d is not None
    assert "grant_rotas" in d["remedio"]
    assert d["causa"] == "sem_permissao"


def test_base_ausente_vira_remedio_diferente():
    erro = fb.DatabaseError('I/O error during "CreateFile (open)" operation')
    d = service.diagnosticar_erro_de_base(erro)
    assert d is not None
    assert "copy_fdb" in d["remedio"]
    assert d["causa"] == "base_ausente"


def test_erro_desconhecido_nao_inventa_remedio():
    assert service.diagnosticar_erro_de_base(fb.DatabaseError("deadlock")) is None
    assert service.diagnosticar_erro_de_base(ValueError("nada a ver")) is None


# ---- /api/health ----------------------------------------------------------

@pytest.mark.erp
def test_health_diz_que_esta_pronto_quando_esta():
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["pronto"] is True
    assert set(body["perfis"]) == {p.value for p in Profile}
    for p in body["perfis"].values():
        assert p["ok"] is True
        assert p["clientes"] > 0


@pytest.mark.erp
def test_health_aponta_o_perfil_quebrado_e_o_remedio(monkeypatch):
    """Sem permissão em uma base, /api/health tem de nomear qual e o que fazer."""
    real = service.connect

    def quebrado(profile):
        if profile is Profile.LOCACAO:
            raise fb.DatabaseError(
                "no permission for SELECT access to TABLE CLIFOR")
        return real(profile)

    monkeypatch.setattr(service, "connect", quebrado)
    body = client.get("/api/health").json()

    assert body["pronto"] is False
    assert body["perfis"]["locacao"]["ok"] is False
    assert "grant_rotas" in body["perfis"]["locacao"]["remedio"]
    assert body["perfis"]["entrega_posterior"]["ok"] is True


# ---- o import devolve a causa, não um 500 --------------------------------

@pytest.mark.erp
def test_import_sem_permissao_devolve_503_com_remedio(monkeypatch):
    def quebrado(profile):
        raise fb.DatabaseError(
            "no permission for SELECT access to TABLE CLIFOR")

    monkeypatch.setattr(service, "connect", quebrado)
    r = client.post("/api/stops/import", json={
        "profile": "locacao", "date": "2026-08-04", "mode": "replanejar"})

    assert r.status_code == 503, "permissão faltando não é erro do cliente nem bug"
    detalhe = r.json()["detail"]
    assert "grant_rotas" in detalhe
    assert "permission" in detalhe.lower() or "permissão" in detalhe.lower()


@pytest.mark.erp
def test_import_com_erro_desconhecido_ainda_sobe(monkeypatch):
    """Só o que sabemos diagnosticar vira 503. O resto continua estourando,
    porque engolir erro desconhecido é como este projeto perde defeito."""
    def quebrado(profile):
        raise fb.DatabaseError("deadlock")

    monkeypatch.setattr(service, "connect", quebrado)
    with pytest.raises(fb.DatabaseError):
        client.post("/api/stops/import", json={
            "profile": "locacao", "date": "2026-08-04", "mode": "replanejar"})
