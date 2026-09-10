import json
import os
import sqlite3
import subprocess
import sys
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
    # AGUA BOA sem prefixo nas duas cidades -- prova que a tolerancia de
    # prefixo (JARDIM/VILA/...) nao reabre a colisao que o raio de cidade
    # ja fecha: o ERP manda "JARDIM AGUA BOA" e so a de Dourados deve valer.
    c.execute("INSERT INTO place VALUES ('bairro','AGUA BOA','',-54.8150,-22.2800)")
    c.execute("INSERT INTO place VALUES ('bairro','AGUA BOA','',-54.6300,-20.5000)")
    c.execute("INSERT INTO place VALUES ('cidade','DOURADOS','',-54.8050,-22.2250)")
    c.execute("INSERT INTO place VALUES ('cidade','CAMPO GRANDE','',-54.6133,-20.4614)")
    # So existe perto de Campo Grande -- nao tem homonimo em Dourados nenhum.
    c.execute("INSERT INTO street VALUES (5,'Rua Distante Unica','RUA DISTANTE UNICA','',"
              "'[[-54.6200,-20.4700],[-54.6100,-20.4700]]',-54.62,-20.47,-54.61,-20.47)")
    c.execute("INSERT INTO street_fts VALUES ('RUA DISTANTE UNICA','',5)")
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


# ---- tolerância de prefixo de bairro (fix round 2/5) -----------------------

def test_bairro_tolera_prefixo_ausente(geo):
    """ERP escreve 'JARDIM AGUA BOA'; o índice só tem 'AGUA BOA'. Prefixos
    quase decorativos (JARDIM/VILA/PARQUE/CONJUNTO/RESIDENCIAL) não podem
    impedir o casamento de bairro."""
    _, r = geo.geocode(_a("RUA QUE NAO EXISTE EM LUGAR NENHUM",
                          bairro="JARDIM AGUA BOA"))
    assert r.source == "bairro"
    assert (r.lon, r.lat) == pytest.approx((-54.8150, -22.2800))


def test_bairro_tolera_prefixo_mas_ainda_respeita_raio_da_cidade(geo_colisao):
    """A tolerância de prefixo não pode reabrir a colisão de cidade que o
    raio geográfico já fecha: 'AGUA BOA' existe sem prefixo em Dourados E em
    Campo Grande; o ERP manda 'JARDIM AGUA BOA' para Dourados e só a
    instância de Dourados pode ser usada."""
    _, r = geo_colisao.geocode(_a("RUA QUE NAO EXISTE", cidade="DOURADOS",
                                  bairro="JARDIM AGUA BOA"))
    assert r.source == "bairro"
    assert (r.lon, r.lat) == pytest.approx((-54.8150, -22.2800))


# ---- particionar por proximidade antes de pontuar (fix round 4/5) ---------
# A revisão achou o defeito raiz: a seleção de nome por fuzzy score
# acontecia SEM nenhuma informação geográfica, então um nome errado só
# coincidentemente melhor pontuado (ou empatado, desfeito por ordem
# arbitrária) vencia o nome certo que existia bem mais perto. Os dois
# fixtures abaixo usam os textos e os scores REAIS medidos contra o índice
# de Dourados (verificados isoladamente com rapidfuzz antes de escrever o
# fixture) para reproduzir os dois achados sem depender do índice real.

def test_rua_que_so_existe_longe_ainda_resolve_com_confianca_baixa(geo_colisao):
    """Uma rua real que só existe longe da cidade do endereço deve resolver
    -- a posição existe e vale mais que nada -- mas nunca com confiança
    alta ou média, que soaria como um acerto local confiável."""
    _, r = geo_colisao.geocode(_a("RUA DISTANTE UNICA", cidade="DOURADOS"))
    assert r.confidence == "low"
    assert r.source == "street_fuzzy"
    assert r.lat == pytest.approx(-20.47, abs=0.02)


@pytest.fixture
def geo_prioridade(tmp_path):
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
    # Caso real 1: "SANTOS DUMONT MARMITARIA" -> "RUA SANTOS DUMONT" (perto,
    # WRatio 82,3) perdia para "AVENIDA ARISTIDES CRISOSTOMO DOS SANTOS"
    # (longe, WRatio 85,5) quando a pontuação não sabia de geografia.
    c.execute("INSERT INTO street VALUES (1,'Rua Santos Dumont','RUA SANTOS DUMONT','',"
              "'[[-54.80,-22.21],[-54.79,-22.21]]',-54.80,-22.21,-54.79,-22.21)")
    c.execute("INSERT INTO street_fts VALUES ('RUA SANTOS DUMONT','',1)")
    c.execute("INSERT INTO street VALUES (2,'Avenida Aristides Crisostomo Dos Santos',"
              "'AVENIDA ARISTIDES CRISOSTOMO DOS SANTOS','',"
              "'[[-56.00,-25.00],[-55.99,-25.00]]',-56.00,-25.00,-55.99,-25.00)")
    c.execute("INSERT INTO street_fts VALUES "
              "('AVENIDA ARISTIDES CRISOSTOMO DOS SANTOS','',2)")
    # Caso real 2: "VEREADOR AGUIAR DE SOUZA" empatava EXATAMENTE (WRatio
    # 85,5) entre "RUA VEREADOR AGUIAR FERREIRA DE SOUZA" (perto, certa) e
    # "RUA DOS SOUZA" (longe, sem relação) -- o desempate por ordem de
    # iteração de um set escolhia a errada.
    c.execute("INSERT INTO street VALUES (3,'Rua Vereador Aguiar Ferreira de Souza',"
              "'RUA VEREADOR AGUIAR FERREIRA DE SOUZA','',"
              "'[[-54.80,-22.24],[-54.79,-22.24]]',-54.80,-22.24,-54.79,-22.24)")
    c.execute("INSERT INTO street_fts VALUES "
              "('RUA VEREADOR AGUIAR FERREIRA DE SOUZA','',3)")
    c.execute("INSERT INTO street VALUES (4,'Rua Dos Souza','RUA DOS SOUZA','',"
              "'[[-56.00,-25.00],[-55.99,-25.00]]',-56.00,-25.00,-55.99,-25.00)")
    c.execute("INSERT INTO street_fts VALUES ('RUA DOS SOUZA','',4)")
    c.execute("INSERT INTO place VALUES ('cidade','DOURADOS','',-54.8050,-22.2250)")
    c.commit(); c.close()
    store = LocalStore(tmp_path / "local.db"); store.init_schema()
    return Geocoder(idx, store)


def test_perto_vence_longe_mesmo_com_score_menor(geo_prioridade):
    """Achado real: 'SANTOS DUMONT MARMITARIA' -> 'RUA SANTOS DUMONT' (perto,
    score 82,3) perdia para 'AVENIDA ARISTIDES CRISOSTOMO DOS SANTOS'
    (longe, score 85,5) porque a pontuação era global, sem geografia."""
    _, r = geo_prioridade.geocode(_a("SANTOS DUMONT MARMITARIA", cidade="DOURADOS"))
    assert r.matched_text == "RUA SANTOS DUMONT"
    assert r.lat == pytest.approx(-22.21, abs=0.02)


def test_empate_de_score_e_desfeito_pela_proximidade(geo_prioridade):
    """Achado real: 'VEREADOR AGUIAR DE SOUZA' empatava exatamente (85,5)
    entre a rua certa (perto) e uma rua completamente diferente (longe); o
    desempate por ordem de iteração de um set escolhia a errada."""
    _, r = geo_prioridade.geocode(_a("VEREADOR AGUIAR DE SOUZA", cidade="DOURADOS"))
    assert r.matched_text == "RUA VEREADOR AGUIAR FERREIRA DE SOUZA"
    assert r.lat == pytest.approx(-22.24, abs=0.02)


@pytest.fixture
def geo_conectivo(tmp_path):
    """Achado real: 'ALAMEDA DAS HORTENCIAS' tem 7 segmentos no índice, 3
    perto de Dourados -- mas a busca FTS incluía 'DAS' como termo, um
    conectivo tão comum em nomes de logradouro que sozinho enchia o
    LIMIT 400 com ruas sem nenhuma relação, e só UM segmento sobrevivia:
    o distante. Este fixture simula isso com muitos decoys que só
    compartilham 'DAS'."""
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
    decoy_pts = json.dumps([[-50.0, -10.0], [-49.99, -10.0]])
    for i in range(410):
        sid = 1000 + i
        c.execute(
            "INSERT INTO street VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, "Rua Das Decoy", "RUA DAS DECOY", "", decoy_pts,
             -50.0, -10.0, -49.99, -10.0))
        c.execute("INSERT INTO street_fts VALUES ('RUA DAS DECOY','',?)", (sid,))
    c.execute("INSERT INTO street VALUES (1,'Alameda das Hortencias',"
              "'ALAMEDA DAS HORTENCIAS','','[[-54.808,-22.213],[-54.807,-22.213]]',"
              "-54.808,-22.213,-54.807,-22.213)")
    c.execute("INSERT INTO street_fts VALUES ('ALAMEDA DAS HORTENCIAS','',1)")
    c.execute("INSERT INTO place VALUES ('cidade','DOURADOS','',-54.8050,-22.2250)")
    c.commit(); c.close()
    store = LocalStore(tmp_path / "local.db"); store.init_schema()
    return Geocoder(idx, store)


def test_conectivo_nao_esgota_o_limite_da_busca(geo_conectivo):
    """'DAS' sozinho não pode consumir o LIMIT 400 da busca FTS com 410
    ruas que não têm nada a ver -- a rua certa (com o erro de digitação
    real do ERP, 'ALAMENDA') tem que ser encontrada mesmo assim."""
    _, r = geo_conectivo.geocode(_a("ALAMENDA DAS HORTENCIAS"))
    assert r.source == "street_fuzzy"
    assert r.confidence in ("high", "medium")
    assert r.lat == pytest.approx(-22.213, abs=0.01)


# ---- determinismo entre processos (fix round 5/5) --------------------------

@pytest.fixture
def streets_db_empate(tmp_path):
    """Duas ruas que empatam EXATAMENTE em WRatio (90,0) para a mesma
    consulta, as duas perto de Dourados -- reproduz sem depender do índice
    real (que pode mudar) o empate que a revisão achou ao vivo:
    'RUA PORTO BELO, ESQ. RUA PORTO IGUAÇU, Q16/L9' geocodificava para
    'RUA PORTO BELO' ou 'RUA PORTO IGUAÇU' dependendo do processo."""
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
    c.execute("INSERT INTO street VALUES (1,'Rua Porto Belo','RUA PORTO BELO','',"
              "'[[-54.80,-22.22],[-54.79,-22.22]]',-54.80,-22.22,-54.79,-22.22)")
    c.execute("INSERT INTO street_fts VALUES ('RUA PORTO BELO','',1)")
    c.execute("INSERT INTO street VALUES (2,'Rua Porto Iguacu','RUA PORTO IGUACU','',"
              "'[[-54.81,-22.23],[-54.80,-22.23]]',-54.81,-22.23,-54.80,-22.23)")
    c.execute("INSERT INTO street_fts VALUES ('RUA PORTO IGUACU','',2)")
    c.execute("INSERT INTO place VALUES ('cidade','DOURADOS','',-54.8050,-22.2250)")
    c.commit(); c.close()
    return idx


def test_geocode_e_deterministico_entre_processos(streets_db_empate, tmp_path):
    """Um teste que geocodifica duas vezes NO MESMO processo não pega este
    bug: a ordem de iteração de um `set` de nomes só varia com
    PYTHONHASHSEED entre processos diferentes. Roda o mesmo geocode em
    cinco interpretadores novos, cada um com uma seed de hash diferente, e
    exige a mesma resposta nos cinco -- o mesmo endereço não pode
    geocodificar diferente dependendo de qual processo rodou a busca."""
    script = f"""
import sys
from pathlib import Path
from api.app.db.local import LocalStore
from api.app.geo.geocoder import Geocoder
from api.app.models import Address

store = LocalStore(Path(sys.argv[1])); store.init_schema()
geo = Geocoder(Path({str(streets_db_empate)!r}), store)
a = Address(logradouro='RUA PORTO BELO, ESQ. RUA PORTO IGUACU, Q16/L9',
            numero=None, bairro=None, cidade='DOURADOS', uf='MS', cep=None, raw='x')
_, g = geo.geocode(a)
print(g.matched_text)
"""
    resultados = set()
    for seed in ("0", "1", "2", "42", "12345"):
        local_db = tmp_path / f"local_seed_{seed}.db"
        env = dict(os.environ, PYTHONHASHSEED=seed)
        out = subprocess.run(
            [sys.executable, "-c", script, str(local_db)],
            capture_output=True, text=True, env=env, check=True, timeout=60,
        )
        resultados.add(out.stdout.strip())
    assert len(resultados) == 1, f"resultados diferentes entre seeds de hash: {resultados}"
