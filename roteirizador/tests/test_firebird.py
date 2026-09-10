import pytest
from api.app.config import Profile, get_settings
from api.app.db.firebird import ErpConnection, ReadOnlyViolation, connect


def _conn() -> ErpConnection:
    s = get_settings()
    return ErpConnection(s.dsn(Profile.LOCACAO), s.fb_user, s.fb_password)


@pytest.mark.parametrize("sql", [
    "UPDATE CLIFOR SET NOME = 'x'",
    "DELETE FROM CLIFOR",
    "INSERT INTO CLIFOR (ID) VALUES (1)",
    "  execute procedure FOO",
    "DROP TABLE CLIFOR",
])
def test_recusa_sql_que_nao_seja_leitura(sql):
    c = ErpConnection.__new__(ErpConnection)      # sem abrir conexão
    with pytest.raises(ReadOnlyViolation):
        c._assert_read_only(sql)


def test_aceita_select_e_with():
    c = ErpConnection.__new__(ErpConnection)
    c._assert_read_only("SELECT 1 FROM RDB$DATABASE")
    c._assert_read_only("  with x as (select 1 from rdb$database) select * from x")


@pytest.mark.erp
def test_conecta_e_conta_locacoes():
    with _conn() as c:
        rows = c.query("SELECT COUNT(*) AS N FROM LOCACAO_PRODUTO")
    assert rows[0]["N"] == 144763


@pytest.mark.erp
def test_query_devolve_dicts_com_chaves_maiusculas():
    with _conn() as c:
        rows = c.query("SELECT FIRST 1 ID_SEQUENCIA, DATA_LOCACAO "
                       "FROM LOCACAO_PRODUTO ORDER BY ID_SEQUENCIA")
    assert set(rows[0]) == {"ID_SEQUENCIA", "DATA_LOCACAO"}


@pytest.mark.erp
def test_parametro_posicional():
    with _conn() as c:
        rows = c.query("SELECT COUNT(*) AS N FROM LOCACAO_PRODUTO WHERE SITUACAO = ?", (1,))
    assert rows[0]["N"] == 332


@pytest.mark.erp
def test_acentuacao_vem_correta():
    with _conn() as c:
        rows = c.query("SELECT FIRST 1 NOME FROM CIDADE WHERE NOME LIKE 'ITAPOR%'")
    assert "Ã" in rows[0]["NOME"] or "A" in rows[0]["NOME"]


@pytest.mark.erp
def test_connect_por_perfil_abre_a_base_de_entrega():
    with connect(Profile.ENTREGA_POSTERIOR) as c:
        rows = c.query("SELECT COUNT(*) AS N FROM ENTREGA_PCAB")
    assert rows[0]["N"] == 233041
