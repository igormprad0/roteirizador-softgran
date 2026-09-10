import json
import sqlite3
import pytest

from api.app.db.local import LocalStore
from api.app.geo.geocoder import Geocoder
from api.app.models import Address

RUA_MG = [[-54.8100, -22.2200], [-54.8000, -22.2200]]


@pytest.fixture
def geo(tmp_path):
    idx = tmp_path / "streets.db"
    c = sqlite3.connect(idx)
    c.executescript("""
      CREATE TABLE street (street_id INTEGER PRIMARY KEY, name TEXT, name_norm TEXT,
        city_norm TEXT, coords_json TEXT, min_lon REAL, min_lat REAL,
        max_lon REAL, max_lat REAL);
      CREATE VIRTUAL TABLE street_fts USING fts5(name_norm, city_norm,
        street_id UNINDEXED, tokenize='unicode61');
      CREATE TABLE housenumber (street_norm TEXT, city_norm TEXT, number TEXT,
        lon REAL, lat REAL);
      CREATE TABLE place (kind TEXT, name_norm TEXT, city_norm TEXT, lon REAL, lat REAL);
    """)
    # city_norm dos ways vem VAZIO de propósito: no índice real só 55 de 55.063
    # linhas têm addr:city. Um fixture que preenchesse isso testaria fantasia.
    c.execute("INSERT INTO street VALUES (1,'Rua Mato Grosso','RUA MATO GROSSO',"
              "'',?,-54.81,-22.22,-54.80,-22.22)", (json.dumps(RUA_MG),))
    c.execute("INSERT INTO street_fts VALUES ('RUA MATO GROSSO','',1)")
    # Marcelino Pires em dois segmentos distantes, como no índice real (20 lá).
    c.execute("INSERT INTO street VALUES (2,'Avenida Marcelino Pires',"
              "'AVENIDA MARCELINO PIRES','','[[-54.82,-22.23],[-54.79,-22.23]]',"
              "-54.82,-22.23,-54.79,-22.23)")
    c.execute("INSERT INTO street_fts VALUES ('AVENIDA MARCELINO PIRES','',2)")
    c.execute("INSERT INTO street VALUES (3,'Avenida Marcelino Pires',"
              "'AVENIDA MARCELINO PIRES','','[[-54.83,-22.28],[-54.80,-22.28]]',"
              "-54.83,-22.28,-54.80,-22.28)")
    c.execute("INSERT INTO street_fts VALUES ('AVENIDA MARCELINO PIRES','',3)")
    c.execute("INSERT INTO housenumber VALUES ('RUA MATO GROSSO','DOURADOS','1973',"
              "-54.8055,-22.2205)")
    c.execute("INSERT INTO housenumber VALUES ('RUA MATO GROSSO','DOURADOS','2500',"
              "-54.8010,-22.2200)")
    c.execute("INSERT INTO place VALUES ('bairro','CENTRO','DOURADOS',-54.8090,-22.2210)")
    c.execute("INSERT INTO place VALUES ('bairro','AGUA BOA','DOURADOS',-54.8150,-22.2800)")
    c.execute("INSERT INTO place VALUES ('cidade','DOURADOS','',-54.8050,-22.2250)")
    c.commit(); c.close()

    store = LocalStore(tmp_path / "local.db"); store.init_schema()
    return Geocoder(idx, store)


def _a(logradouro, **kw):
    base = dict(logradouro=logradouro, numero=None, bairro=None, cidade="DOURADOS",
                uf="MS", cep=None, raw=logradouro)
    base.update(kw)
    return Address(**base)


def test_housenumber_exato_tem_prioridade(geo):
    _, r = geo.geocode(_a("RUA MATO GROSSO, 1973"))
    assert r.source == "street_exact"
    assert r.confidence == "high"
    assert (r.lon, r.lat) == pytest.approx((-54.8055, -22.2205))


def test_rua_exata_com_numero_desconhecido_interpola(geo):
    _, r = geo.geocode(_a("RUA MATO GROSSO, 500"))
    assert r.source in ("street_exact", "street_fuzzy")
    assert -54.8100 <= r.lon <= -54.8000
    assert r.lat == pytest.approx(-22.2200)


def test_fuzzy_corrige_erro_de_digitacao(geo):
    _, r = geo.geocode(_a("RUA MATO GROSO, 500"))
    assert r.source in ("street_fuzzy", "street_exact")
    assert r.confidence == "medium"


def test_sem_numero_cai_no_ponto_medio_da_via(geo):
    _, r = geo.geocode(_a("AVENIDA MARCELINO PIRES"))
    assert r.source in ("street_mid", "street_fuzzy", "street_exact")
    assert r.lat == pytest.approx(-22.23)


def test_rua_desconhecida_cai_no_bairro(geo):
    _, r = geo.geocode(_a("RUA QUE NAO EXISTE EM LUGAR NENHUM", bairro="CENTRO"))
    assert r.source == "bairro"
    assert r.confidence == "low"
    assert (r.lon, r.lat) == pytest.approx((-54.8090, -22.2210))


def test_sem_rua_e_sem_bairro_cai_na_cidade(geo):
    _, r = geo.geocode(_a("HECTARES"))
    assert r.source == "cidade"
    assert r.confidence == "low"


def test_cidade_desconhecida_resulta_em_failed(geo):
    _, r = geo.geocode(_a("QUALQUER COISA", cidade="CIDADE INEXISTENTE", bairro=None))
    assert r.source == "none"
    assert r.confidence == "failed"


def test_segunda_chamada_vem_do_cache(geo):
    k1, r1 = geo.geocode(_a("RUA MATO GROSSO, 1973"))
    k2, r2 = geo.geocode(_a("R. Mato Grosso 1973"))
    assert k1 == k2
    assert r2.source == "cache"
    assert (r2.lon, r2.lat) == (r1.lon, r1.lat)


def test_pin_manual_vence_o_cache_e_o_matching(geo):
    key, _ = geo.geocode(_a("RUA MATO GROSSO, 1973"))
    geo.pin(key, -54.7000, -22.1000)
    _, r = geo.geocode(_a("RUA MATO GROSSO, 1973"))
    assert r.source == "manual"
    assert (r.lon, r.lat) == (-54.7000, -22.1000)


def test_endereco_vazio_nao_explode(geo):
    _, r = geo.geocode(_a(""))
    assert r.confidence in ("low", "failed")


# ---- escolha de segmento e interpolação (§ dados reais da Task 5) ----------

def test_escolhe_o_segmento_da_via_mais_perto_do_bairro(geo):
    """A Marcelino Pires real são 20 segmentos somando 9,3 km. Pegar o errado
    erra por quilômetros; o bairro é o que desempata."""
    _, norte = geo.geocode(_a("AVENIDA MARCELINO PIRES", bairro="CENTRO"))
    _, sul = geo.geocode(_a("AVENIDA MARCELINO PIRES", bairro="AGUA BOA"))
    assert norte.lat == pytest.approx(-22.23)
    assert sul.lat == pytest.approx(-22.28)


def test_sem_bairro_conhecido_ancora_na_cidade(geo):
    _, r = geo.geocode(_a("AVENIDA MARCELINO PIRES", bairro="BAIRRO INEXISTENTE"))
    # centroide de DOURADOS é -22.2250, mais perto do segmento norte
    assert r.lat == pytest.approx(-22.23)


def test_usa_o_numero_conhecido_mais_proximo(geo):
    """Sem o número exato, o vizinho mais próximo posiciona muito melhor que
    o ponto médio — e MAX(number) não serve porque 94% das ruas não têm número."""
    _, r = geo.geocode(_a("RUA MATO GROSSO, 2400"))
    assert (r.lon, r.lat) == pytest.approx((-54.8010, -22.2200))   # vizinho 2500
    _, perto_do_1973 = geo.geocode(_a("RUA MATO GROSSO, 1980"))
    assert (perto_do_1973.lon, perto_do_1973.lat) == pytest.approx((-54.8055, -22.2205))


def test_via_sem_nenhum_numero_cai_no_meio_do_segmento_escolhido(geo):
    _, r = geo.geocode(_a("AVENIDA MARCELINO PIRES, 500", bairro="AGUA BOA"))
    assert r.lat == pytest.approx(-22.28)      # segmento certo, não a via inteira


def test_nao_usa_city_norm_do_way_para_filtrar(geo):
    """city_norm dos ways está vazio no índice real; se o geocoder filtrasse
    por ele, não acharia rua nenhuma."""
    _, r = geo.geocode(_a("RUA MATO GROSSO, 1973", cidade="DOURADOS"))
    assert r.confidence in ("high", "medium")
    assert r.source != "cidade"


# ---- escopo de cidade em TODA consulta -------------------------------------
# O fixture abaixo replica as colisões reais do índice: mesmo nome de rua,
# mesmo número de porta e mesmo nome de bairro existindo em outro município.

@pytest.fixture
def geo_colisao(tmp_path):
    idx = tmp_path / "streets.db"
    c = sqlite3.connect(idx)
    c.executescript("""
      CREATE TABLE street (street_id INTEGER PRIMARY KEY, name TEXT, name_norm TEXT,
        city_norm TEXT, coords_json TEXT, min_lon REAL, min_lat REAL,
        max_lon REAL, max_lat REAL);
      CREATE VIRTUAL TABLE street_fts USING fts5(name_norm, city_norm,
        street_id UNINDEXED, tokenize='unicode61');
      CREATE TABLE housenumber (street_norm TEXT, city_norm TEXT, number TEXT,
        lon REAL, lat REAL);
      CREATE TABLE place (kind TEXT, name_norm TEXT, city_norm TEXT, lon REAL, lat REAL);
    """)
    # Mesma rua em Dourados (-54.81/-22.22) e em Campo Grande (-54.61/-20.46).
    c.execute("INSERT INTO street VALUES (1,'Rua Mato Grosso','RUA MATO GROSSO','',"
              "'[[-54.8100,-22.2200],[-54.8000,-22.2200]]',-54.81,-22.22,-54.80,-22.22)")
    c.execute("INSERT INTO street_fts VALUES ('RUA MATO GROSSO','',1)")
    c.execute("INSERT INTO street VALUES (2,'Rua Mato Grosso','RUA MATO GROSSO','',"
              "'[[-54.6100,-20.4600],[-54.6000,-20.4600]]',-54.61,-20.46,-54.60,-20.46)")
    c.execute("INSERT INTO street_fts VALUES ('RUA MATO GROSSO','',2)")
    # Rua numerada: todos os tokens são genéricos ou dígitos.
    c.execute("INSERT INTO street VALUES (3,'Alameda 5','ALAMEDA 5','',"
              "'[[-54.8300,-22.2400],[-54.8200,-22.2400]]',-54.83,-22.24,-54.82,-22.24)")
    c.execute("INSERT INTO street_fts VALUES ('ALAMEDA 5','',3)")
    # Mesmo número de porta nas duas cidades; só o de CG tem city_norm.
    c.execute("INSERT INTO housenumber VALUES ('RUA MATO GROSSO','DOURADOS','1973',"
              "-54.8055,-22.2205)")
    c.execute("INSERT INTO housenumber VALUES ('RUA MATO GROSSO','CAMPO GRANDE','1973',"
              "-54.6055,-20.4605)")
    # CENTRO existe nas duas; o de CG vem primeiro na varredura.
    c.execute("INSERT INTO place VALUES ('bairro','CENTRO','',-54.6133,-20.4614)")
    c.execute("INSERT INTO place VALUES ('bairro','CENTRO','',-54.8112,-22.2279)")
    c.execute("INSERT INTO place VALUES ('cidade','DOURADOS','',-54.8050,-22.2250)")
    c.execute("INSERT INTO place VALUES ('cidade','CAMPO GRANDE','',-54.6133,-20.4614)")
    c.commit(); c.close()
    store = LocalStore(tmp_path / "local.db"); store.init_schema()
    return Geocoder(idx, store)


def test_numero_de_porta_de_outra_cidade_nao_e_usado(geo_colisao):
    """O caso real: 'RUA MATO GROSSO, 1973' em Dourados resolvia a 145 km."""
    _, r = geo_colisao.geocode(_a("RUA MATO GROSSO, 1973", cidade="DOURADOS"))
    assert r.lat == pytest.approx(-22.2205, abs=0.02)
    assert r.lon == pytest.approx(-54.8055, abs=0.02)


def test_bairro_homonimo_de_outra_cidade_nao_e_usado(geo_colisao):
    """CENTRO existe em 10 municípios do extrato real."""
    _, r = geo_colisao.geocode(_a("RUA QUE NAO EXISTE", cidade="DOURADOS",
                                  bairro="CENTRO"))
    assert r.source == "bairro"
    assert r.lat == pytest.approx(-22.2279, abs=0.02)


def test_segmento_de_outra_cidade_nao_e_escolhido(geo_colisao):
    _, r = geo_colisao.geocode(_a("RUA MATO GROSSO", cidade="DOURADOS"))
    assert r.lat == pytest.approx(-22.22, abs=0.05)


def test_rua_numerada_continua_encontravel(geo_colisao):
    """RUA 6, ALAMEDA 1..9 e afins são convenção de loteamento brasileiro.
    O filtro de palavras genéricas não pode torná-las inencontráveis."""
    _, r = geo_colisao.geocode(_a("ALAMEDA 5", cidade="DOURADOS"))
    assert r.source in ("street_exact", "street_fuzzy", "street_mid")
    assert r.lat == pytest.approx(-22.24, abs=0.02)


def test_candidato_longe_demais_da_cidade_e_recusado(geo_colisao):
    """Sem nada plausível perto, cai no centroide da cidade — não vai buscar
    a 200 km e devolver `medium` como se tivesse acertado."""
    _, r = geo_colisao.geocode(_a("RUA MATO GROSSO, 1973", cidade="CAMPO GRANDE"))
    assert r.lat == pytest.approx(-20.46, abs=0.05)
