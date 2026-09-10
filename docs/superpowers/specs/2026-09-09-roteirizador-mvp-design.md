# Roteirizador MVP — Design

**Data:** 2026-09-09
**Status:** Aprovado
**Objetivo de negócio:** validar, com dois clientes reais, que roteirização otimizada é uma feature vendável do ERP Softgran Empresarial — e sair com uma demo apresentável que gere upsell.

---

## 1. Contexto verificado

### 1.1 O sistema

ERP Delphi (Softgran Empresarial), banco Firebird 3.0. Módulos em `prj/Src/`: `SVComercial`, `Frota`, `SVFinanca`, `SVARH`, `SVCfg`, `ServerPAF`.

As duas bases fornecidas (`cliente locação.FDB`, 896 MB; `cliente entrega posterior.FDB`, 3,96 GB) têm **schema idêntico** — 558 tabelas de usuário, zero diferença. São o mesmo ERP com módulos diferentes em uso. Uma única camada de integração serve os dois.

### 1.2 Tabelas relevantes (colunas verificadas via `RDB$RELATION_FIELDS`)

**Entrega posterior:**

| Tabela | Papel | Colunas-chave |
|---|---|---|
| `ENTREGA_CAB` | pedido de entrega + endereço | `ID_CONTROLE`, `ID_CLIENTE`, `DATA`, `VENCIMENTO`, `SITUACAO`, `TIPO_ENTREGA`, `ORIGEM_REGISTRO`, `ID_ORIGEM_REGISTRO`, `LOGRADOURO`, `CLIENTE_NOME`, `CLIENTE_ENDERECO`, `CLIENTE_BAIRRO`, `CLIENTE_CIDADE`, `CLIENTE_UF`, `ID_CIDADE`, `ID_BAIRRO` |
| `ENTREGA_DET` | itens do pedido | `ID_CONTROLE`, `ID_SEQUENCIA`, `ID_PRODUTO`, `QTD_ENTREGAR`, `QTD_ENTREGUE`, `SALDO` |
| `ENTREGA_PCAB` | **agendamento** (a parada) | `ID_CONTROLE`, `ID_ENTREGA`, `DATA`, `HORA`, `ID_VEICULO`, `ID_LOTE`, `SITUACAO`, `TIPO_ENTREGA`, `FLAG_DEV_VENDAS`, `DATA_CANCEL` |
| `ENTREGA_PDET` | itens agendados | `ID_CONTROLE`, `ID_SEQUENCIA`, `ID_PRODUTO`, `QUANTIDADE` |
| `ENTREGA_LOG` | auditoria | `ID_ENTREGA`, `DATA_HORA`, `USUARIO`, `OBSERVACAO` |

**Locação:**

| Tabela | Papel | Colunas-chave |
|---|---|---|
| `LOCACAO_PRODUTO` | locação de 1 equipamento | `ID_SEQUENCIA`, `DATA_LOCACAO`, `DOCUMENTO`, `ID_CLIENTE`, `ENDERECO_ENTREGA` (texto livre), `ID_PRODUTO`, `QUANTIDADE`, `SITUACAO`, `ORIGEM_REGISTRO`, `ID_ORIGEM_REGISTRO`, `DATA_DEVOLUCAO` |

`SITUACAO`: `1` = em locação (aberto), `2` = devolvido. Confirmado por distribuição (144.431 em `2`, 332 em `1`) cruzada com `DATA_DEVOLUCAO IS NULL`.

### 1.2.1 Semântica dos códigos (decodificada do fonte Delphi)

`ENTREGA_CAB.SITUACAO` — de `uObjEntregaPosterior.pas:380-453`, que promove `2→3` quando todos os `ENTREGA_DET` ficam com `SALDO <= 0`, e rebaixa `3→2` quando algum volta a ter `SALDO > 0`:

| Valor | Significado | Ocorrências |
|---|---|---|
| `1` | inicial / em preparação | 52 |
| `2` | **aberto — há saldo a entregar** | 406 |
| `3` | totalmente entregue | 158.051 |

`ENTREGA_PCAB.SITUACAO` — de `uObjEntregaPosterior.pas:655,735,777` e `ufrmMEntregaPosteriorAgenda.pas:320`:

| Valor | Significado | Ocorrências |
|---|---|---|
| `1` | **agendado, pendente de execução** | 0 |
| `2` | baixado / executado | 231.142 |
| `3` | cancelado (grava `DATA_CANCEL`, `USER_CANCEL`, `RAZAO_CANCEL`) | 1.899 |

`ENTREGA_CAB.TIPO_ENTREGA` — de `udmdComercialGeneric.pas:5990-5997`:

| Valor | Rótulo na tela | Ocorrências | Roteirizar? |
|---|---|---|---|
| `1` | Agendar Para Data Específica | 154 | sim |
| `2` | **Cliente Irá Retirar a Mercadoria** | 25 | **não — excluir** |
| `3` | Aguardar Autorização do Cliente | 110.432 | sim |
| `9` | Outros | 47.888 | sim |

**Consequência para o filtro de importação, e é importante:** as bases fornecidas são snapshots históricos — **`ENTREGA_PCAB.SITUACAO = 1` tem zero linhas**. Tudo já foi executado ou cancelado. Um filtro de produção (`SITUACAO = 1`) retornaria vazio na demo.

O importador aceita portanto um `mode`:
- `mode="producao"` → `SITUACAO = 1` (o que está pendente de verdade)
- `mode="replanejar"` → `SITUACAO <> 3` na data escolhida (replaneja um dia histórico como se estivesse sendo planejado agora)

A demo roda em `replanejar`. Isso também é o que viabiliza o comparativo do §5.4: o dia histórico traz a alocação real de veículo e a hora, que é exatamente o baseline.

Em ambos os modos, excluir `ENTREGA_CAB.TIPO_ENTREGA = 2` (cliente retira no balcão — não gera parada).

**Comuns:** `CLIFOR` (cadastro: `ENDERECO`, `NUMERO`, `BAIRRO`, `CIDADE`, `UF`, `CEP`, `COMPLEMENTO`, `ID_CIDADE`, `ID_BAIRRO`), `CLIFOR_ESTAB` (endereços alternativos de entrega), `CIDADE`, `BAIRRO`, `VEICULO`, `ITEM` (PK `ID_ITEM`), `ESTABELECIMENTO`.

### 1.3 Volumes reais

| | Entrega posterior | Locação |
|---|---|---|
| Paradas/dia (média recente) | 11–17 | ~46 (684 locações + 678 devoluções / 30 dias) |
| Pico registrado | 215 (2024-12-09) | — |
| Veículos usados/dia | 5–7 | 1 |
| Registros históricos | 233.041 `ENTREGA_PCAB` | 144.763 `LOCACAO_PRODUTO` |
| Locações em aberto | — | 1.381 (332 com +30 dias = "vencidas") |
| Clientes cadastrados | 33.928 | 25.107 |

Ambos os clientes operam em **Dourados-MS** (~80% dos `CLIFOR`). Um extrato OSM do Centro-Oeste cobre a operação inteira, incluindo Campo Grande, Maracaju, Itaporã e Ponta Porã.

### 1.4 As três restrições que definem o projeto

1. **Não existe latitude/longitude em lugar nenhum do ERP.** Varredura em `RDB$RELATION_FIELDS` das 558 tabelas por `%LAT%`, `%LONG%`, `%GPS%`, `%GEO%`, `%COORD%` retornou zero campos geográficos. Geocodificação é componente obrigatório, não acessório.

2. **Endereços são texto livre e sujos.** Amostras reais de `LOCACAO_PRODUTO.ENDERECO_ENTREGA`: `"HECTARES"`, `"LOCALIZACAO PORTAL"`, `"JM EVENTO - ROD DDOS A ITAPORA"`, `"IPE ROSA 115 Q 18 L 01 ECOVILLE 1 -HASSAN"`. Cidade gravada como `DOURADOS` / `DDOS` / `DORUADOS` / `DOURADOS - MS`. Normalização e fallback em cascata são obrigatórios.

3. **`VEICULO` não é confiável como frota.** Todas as capacidades (`PESO_LIQ`, `PESO_BRUTO`, `CAP_LOT`, `CAPAC_TRACAO`) são `NULL` nas duas bases. A tabela é usada como caixinha de despacho: entre os 37 registros do cliente de entrega há `DEVOLUÇÃO`, `PRÓPRIO/RETIRA`, `ENTREGA DUVIDOSA`, `DEPOSITO JESSICA`, `MADEGRAN`, `A - VENDEDOR PACOTE`. Só ~8 são caminhão. O cliente de locação tem 2 registros (`GERAL` e `MB 1513 KTD-36454`). **A frota é configurada no roteirizador**, opcionalmente semeada a partir de `VEICULO`.

---

## 2. Decisão sobre os motores open source

| Motor | Papel no MVP | Justificativa |
|---|---|---|
| **OSRM** | ✅ motor de rota | Serviço `/table` (matriz) é o mais rápido dos três, e matriz é ~90% do custo de um VRP. Docker trivial, integração nativa com VROOM. Usa `/table`, `/route`, `/nearest`. |
| **Valhalla** | 🔜 upgrade planejado | Perfil `truck` respeita altura/peso/vias proibidas — relevante para caminhão de caçamba. Custa matriz mais lenta e tiles a configurar. Fora do MVP. |
| **ORS** | ❌ descartado | Mais pesado (Java + GraphHopper), e seu endpoint `/v2/optimization` é VROOM por baixo. Usar ORS seria adicionar uma camada sem ganho. |

**Nenhum dos três resolve VRP.** Os três respondem "qual o caminho de A até B" e "qual a matriz de custos entre N pontos". Nenhum decide qual caminhão leva o quê, em que ordem. Isso exige um solver por cima.

**Solver: VROOM.** Escolhido porque seu modelo de `job` aceita `delivery` (carga que sai do caminhão) e `pickup` (carga que entra) **no mesmo job**, e o solver garante que a ocupação nunca excede a capacidade *em nenhum ponto da rota*. Isso é exatamente o requisito "o caminhão vai cheio e volta cheio", resolvido no modelo. Também oferece `shipment` (par coleta→entrega vinculado), `priority` (0–100), `time_windows`, `service`, `skills` e devolve `unassigned` para o que não coube.

Alternativa considerada e descartada: OR-Tools. Mais flexível, mas exige escrever o modelo em código e não tem integração pronta com OSRM — custo maior para o mesmo resultado no escopo do MVP.

---

## 3. Arquitetura

### 3.1 Containers (Docker Compose, WSL2)

```
osrm     osrm-backend  — extrato centro-oeste-latest.osm.pbf, algoritmo CH
vroom    vroom-express — aponta para osrm:5000
api      FastAPI (Python 3.11) — ERP, geocode, orquestração, exportação
web      estático (Leaflet), servido pela api
```

SQLite local (arquivo em volume) para cache de geocode, frota, depósito e rotas salvas. Sem container de banco: menos uma peça, e suficiente para o volume (dezenas de milhares de endereços).

**Preparação do OSRM** (uma vez, fora do ciclo de request):
`osrm-extract -p car.lua` → `osrm-partition` não necessário; usar `osrm-contract` (CH) porque o grafo é estático e CH dá `/table` mais rápido → `osrm-routed --algorithm ch`.

### 3.2 Acesso ao ERP

**Estritamente somente leitura.** Conexão via `firebird-driver` com usuário dedicado. Nenhum `INSERT`/`UPDATE`/`DELETE` no Firebird do cliente. Tudo que o roteirizador produz vai para o SQLite local.

Migração futura para tabelas `ROTA_*` no Firebird está fora do MVP, mas o `db/local.py` isola o acesso para que a troca seja localizada.

### 3.3 Estrutura de código

```
roteirizador/
  docker-compose.yml
  osm/                            # .pbf + artefatos OSRM + índice de ruas
  api/
    app/
      main.py                     # FastAPI, endpoints HTTP
      config.py                   # perfis (locacao | entrega_posterior), paths, DSNs
      models.py                   # dataclasses do domínio
      db/
        firebird.py               # conexão read-only ao ERP
        local.py                  # SQLite: cache, frota, rotas
      erp/
        base.py                   # interface StopSource
        locacao.py
        entrega_posterior.py
      geo/
        normalize.py              # limpeza de endereço brasileiro
        index_builder.py          # .pbf -> índice de ruas em SQLite FTS5
        geocoder.py               # cascata de matching
      routing/
        osrm.py                   # cliente /table /route /nearest
        vroom.py                  # monta payload, parseia solução
        optimizer.py              # orquestra paradas -> solução
        baseline.py               # custo da ordem atual do ERP
      export/
        romaneio.py               # PDF por caminhão
        maps_link.py              # deep link Waze / Google Maps
  web/
    index.html  app.js  style.css
  tests/
```

Três interfaces, cada uma compreensível e testável sem ler as outras:

```python
class StopSource(Protocol):
    def fetch(self, target_date: date, mode: ImportMode) -> list[Stop]: ...
    def baseline_order(self, stops: list[Stop]) -> list[BaselineTrip]: ...

class Geocoder(Protocol):
    def geocode(self, address: Address) -> GeoResult: ...
    def pin(self, address_key: str, lat: float, lon: float) -> None: ...

class Optimizer(Protocol):
    def solve(self, stops: list[Stop], fleet: list[Vehicle], depot: Depot) -> Solution: ...
```

Adicionar um terceiro módulo do ERP no futuro = escrever um `StopSource` novo. Nada mais muda.

### 3.4 Modelo de domínio

```python
@dataclass(frozen=True)
class Address:
    logradouro: str | None
    numero: str | None
    bairro: str | None
    cidade: str | None
    uf: str | None
    cep: str | None
    raw: str                      # texto original íntegro, para fuzzy de última instância

@dataclass
class Stop:
    external_id: str              # "EP:162886" | "LOC:153543" — estável, referencia o ERP
    kind: Literal["delivery", "pickup", "exchange"]
    cliente_id: int
    cliente_nome: str
    address: Address
    amount: int                   # unidades ocupadas no caminhão
    priority: int                 # 0-100
    service_seconds: int
    due_date: date | None
    days_overdue: int
    doc: str | None               # DOCUMENTO / ID_ORIGEM_REGISTRO, p/ rastrear no ERP
    notes: str

@dataclass
class GeoResult:
    lat: float
    lon: float
    confidence: Literal["high", "medium", "low", "failed"]
    source: Literal["manual", "cache", "street_exact", "street_fuzzy", "bairro", "cidade", "none"]
    matched_text: str | None
    score: float                  # 0-100

@dataclass
class Vehicle:
    id: str                       # "MB1513#1" — inclui número da viagem
    label: str
    placa: str | None
    capacity: int
    shift_start_s: int            # segundos desde 00:00
    shift_end_s: int
    trip_index: int               # ver §5.3, múltiplas viagens
    erp_id_veiculo: int | None

@dataclass
class Solution:
    routes: list[VehicleRoute]    # VehicleRoute: vehicle_id, steps[], distance_m, duration_s, geometry
    unassigned: list[Unassigned]  # stop_external_id + motivo
    total_distance_m: int
    total_duration_s: int
```

---

## 4. Geocodificação

O componente de maior risco do MVP. Sem coordenada não há rota.

### 4.1 Decisão: geocoder próprio a partir do `.pbf`

**Nominatim self-hosted foi descartado**: o import do Centro-Oeste leva horas e consome dezenas de GB — incompatível com o prazo. Photon depende de um dump Nominatim, mesmo problema. Nominatim público tem limite de 1 req/s (25 mil clientes = ~7 horas) e cria dependência externa numa demo.

Em vez disso, `geo/index_builder.py` extrai do **mesmo `.pbf` que alimenta o OSRM**:
- todas as `way` com `highway=*` e `name` — nome, geometria (lista de nós), cidade inferida
- todos os nós e ways com `addr:housenumber` + `addr:street`
- polígonos/pontos de bairro (`place=suburb|neighbourhood`) e cidade (`place=city|town|village`)

Grava em SQLite com FTS5 sobre o nome normalizado da via. Consulta em milissegundos, offline, sem dependência de rede.

### 4.2 Normalização (`geo/normalize.py`)

Aplicada antes de qualquer match: maiúsculas sem acento; expansão de abreviações (`R.`/`RUA`, `AV`/`AVENIDA`, `ROD`/`RODOVIA`, `AL`/`ALAMEDA`, `TRAV`/`TRAVESSA`); extração do número (`, 1973` / `N° 665` / `Nº 665` / `1470,`); descarte de ruído de lote/quadra (`Q 13 LT 06`, `LOTE 06 QUADRA 09`) guardado como complemento; canonicalização de cidade (`DDOS`, `DORUADOS`, `DOURADOS - MS`, `DOURADOS-MS` → `DOURADOS/MS`).

### 4.3 Cascata

| Ordem | Estratégia | Confiança |
|---|---|---|
| 1 | Pin manual gravado pelo usuário | `high` |
| 2 | Cache de execução anterior | herda a original |
| 3 | Via com nome exato + número interpolado na geometria | `high` |
| 4 | Via por fuzzy match (`rapidfuzz`, score ≥ 88) + número | `medium` |
| 5 | Via por fuzzy match (score ≥ 75) sem número → ponto médio da via | `medium` |
| 6 | Centroide do bairro | `low` |
| 7 | Centroide da cidade | `low` |
| 8 | Nenhum match → `failed`, entra na fila de revisão | `failed` |

Para paradas de locação, o endereço é montado de `LOCACAO_PRODUTO.ENDERECO_ENTREGA` **com fallback para o cadastro em `CLIFOR`** quando o texto livre não resolve — o cadastro costuma ser mais limpo.

Toda coordenada resolvida grava no cache com `confidence` e `source`. Pins arrastados pelo usuário gravam com `source="manual"` e nunca são sobrescritos automaticamente.

### 4.4 Expectativa honesta

Com a qualidade de endereço observada, a taxa esperada de acerto automático em `high`+`medium` é de **70–85%** na primeira execução. O restante cai na fila de revisão manual. Após algumas semanas de uso o cache cobre a cauda, porque a base de clientes se repete.

Isso é tratado como característica do produto, não como defeito: no mapa cada pin tem cor por confiança, o usuário arrasta o errado, e a correção é permanente. O argumento de venda é "o sistema aprende os endereços da sua operação".

---

## 5. Modelagem VRP

### 5.1 Os dois módulos no mesmo motor

| | Entrega posterior | Locação |
|---|---|---|
| Fonte | `ENTREGA_PCAB` da data + `ENTREGA_CAB` + `ENTREGA_PDET`, filtro do §1.2.1 | `LOCACAO_PRODUTO` |
| Entregas | parada agendada → job com `delivery` | `DATA_LOCACAO = D` e `SITUACAO = 1` → job com `delivery` |
| Coletas | `FLAG_DEV_VENDAS` marcado → job com `pickup` | `SITUACAO = 1` e (`DATA_DEVOLUCAO ≤ D` ou vencida) → job com `pickup` |
| Troca no mesmo local | — | retira cheia + deixa vazia → `shipment` VROOM |
| Prioridade | proximidade de `ENTREGA_CAB.VENCIMENTO` | dias de atraso da locação |

Paradas que não couberem voltam em `unassigned`. Isso é resposta útil, não erro: *"estas 8 não cabem hoje"*.

### 5.2 Capacidade e tempo de serviço

`VEICULO` não tem capacidade utilizável (§1.4), então a capacidade é configurada na tela de frota.

- **Locação:** unidade natural = 1 equipamento (caçamba). `amount = QUANTIDADE`.
- **Entrega posterior:** as quantidades de `ENTREGA_PDET` estão em unidades heterogêneas (m³ de areia, milheiro de tijolo, unidade). Somá-las produz número sem significado físico. **No MVP, `amount = 1` por parada** e a capacidade é expressa em paradas por viagem. Somar `QUANTIDADE` fica para depois de o cliente definir uma unidade de carga comum — não há como acertar isso sem essa informação.

Tempo de serviço padrão, configurável: entrega 10 min, coleta 15 min, troca 25 min.

### 5.3 Múltiplas viagens por caminhão

Restrição real do caso de locação: um caminhão poliguindaste transporta **uma** caçamba por vez. Com `capacity = 1`, uma rota que entrega em A e coleta em B precisa passar pelo depósito no meio. VROOM não modela múltiplas viagens numa rota.

Solução: o `Vehicle` do domínio carrega `trip_index`, e cada caminhão físico é expandido em N veículos VROOM (`MB1513#1`, `MB1513#2`, …) com janelas de turno sequenciais, todos partindo e terminando no depósito. Na apresentação, as viagens são reagrupadas sob o caminhão físico. N é configurável por caminhão (padrão 6).

Essa é a razão pela qual `Vehicle.id` inclui o número da viagem e existe `erp_id_veiculo` separado.

### 5.4 Baseline para comparação

Objetivo: número defensável, não número bonito.

- **Entrega posterior:** o ERP já grava `ENTREGA_PCAB.ID_VEICULO` e `HORA`. Baseline = agrupar por `ID_VEICULO`, ordenar por `HORA` e depois `ID_CONTROLE`. É literalmente a rota que a operação executou.
- **Locação:** não há veículo nem ordem no ERP. Baseline = ordem de `ID_SEQUENCIA` (ordem de lançamento), particionada sequencialmente entre a mesma quantidade de veículos/viagens. Corresponde ao que o operador faz na prática: trabalhar de cima para baixo na lista impressa.

Ambas as sequências são medidas no OSRM com o mesmo perfil e o mesmo tempo de serviço da rota otimizada. Só o que muda entre baseline e otimizado é a ordem e a alocação.

A limitação do baseline de locação é registrada na UI, junto ao número.

---

## 6. Interface e fluxo

### 6.1 Fluxo do usuário

1. Selecionar perfil (`locacao` | `entrega_posterior`) e data → **Importar**
2. Ver paradas na lista e no mapa. Pins coloridos por confiança de geocodificação. Arrastar os errados.
3. Configurar frota: quais veículos entram, capacidade, viagens, janela de turno, depósito.
4. **Otimizar** → rotas coloridas por caminhão, sequência numerada, ETA por parada, km e tempo por rota.
5. Exportar: romaneio PDF por caminhão, deep link Waze/Google Maps da sequência, CSV.

### 6.2 Painel "Hoje vs Otimizado"

O entregável comercial. Compara o baseline (§5.4) com a solução, em:
- km rodados
- horas em rota
- número de veículos/viagens usados
- estimativa em R$/mês (custo por km configurável)

### 6.3 Endpoints

```
GET    /api/profiles
POST   /api/stops/import            {profile, date, mode}    -> Stop[] com GeoResult
                                    mode: "producao" | "replanejar"  (§1.2.1)
POST   /api/geocode/pin             {address_key, lon, lat}  -> grava manual no cache
                                    a chave é o endereço normalizado, não o pedido:
                                    corrigir um pino conserta todos os pedidos
                                    daquele mesmo endereço
GET    /api/fleet?profile=
PUT    /api/fleet
GET    /api/depot?profile=
PUT    /api/depot
POST   /api/optimize                {profile, date, vehicles}-> Solution + Baseline
GET    /api/runs/{id}
GET    /api/runs/{id}/romaneio.pdf?vehicle=
GET    /api/runs/{id}/maps-link?vehicle=
GET    /api/runs/{id}/export.csv
```

### 6.4 Persistência local (SQLite)

```
geocode_cache(address_key PK, lat, lon, confidence, source, matched_text, score, is_manual, updated_at)
street_fts(name_norm, street_id)                -- FTS5
street_geom(street_id PK, name, city, coords_json, bbox)
place_centroid(kind, name_norm, city_norm, lat, lon)
fleet(id PK, profile, label, placa, capacity, trips, shift_start_s, shift_end_s, enabled, erp_id_veiculo)
depot(profile PK, label, lat, lon, address)
route_run(id PK, profile, target_date, created_at, payload_json)
```

`route_run.payload_json` guarda a resposta inteira de `/api/optimize` — paradas, rotas, não atendidas, totais e comparativo. Normalizar isso em `route_leg`/`route_unassigned` não traria nada no MVP: nada consulta rotas por junção, só se recupera a execução inteira para redesenhar o mapa ou reemitir o romaneio.

`address_key` é o hash do endereço normalizado, não o ID do registro — assim o cache é compartilhado entre pedidos do mesmo cliente/local.

---

## 7. Testes

| Alvo | Tipo | Conteúdo |
|---|---|---|
| `geo/normalize` | unitário | Amostras reais extraídas das bases, incluindo os casos degenerados (`"HECTARES"`, `"JM EVENTO - ROD DDOS A ITAPORA"`, `"IPE ROSA 115 Q 18 L 01 ECOVILLE 1 -HASSAN"`) |
| `geo/geocoder` | unitário | Índice de ruas sintético em memória; verifica a cascata degrau a degrau e a precedência do pin manual |
| `erp/locacao`, `erp/entrega_posterior` | integração | Contra cópias read-only dos `.fdb` reais; assertivas sobre contagens conhecidas |
| `routing/vroom` | unitário | Construção do payload: capacidade, expansão de viagens, prioridade, `shipment` |
| `routing/optimizer` | integração | Containers vivos; um cenário pequeno com solução conhecida |
| `routing/baseline` | unitário | Baseline e otimizado medidos com mesmo perfil e mesmo tempo de serviço |

---

## 8. Fora do escopo do MVP

Escrita de volta no Firebird do cliente (`ENTREGA_PCAB.ID_VEICULO`, sequência da rota) · app do motorista · rastreamento em tempo real · trânsito dependente do horário · restrições de caminhão (altura/peso — depende de Valhalla) · múltiplos depósitos · autenticação e multi-tenancy · Valhalla · ORS.

---

## 9. Riscos

| Risco | Impacto | Mitigação |
|---|---|---|
| Taxa de geocodificação abaixo do esperado | Demo fraca | Cascata com fallback até centroide de cidade; fila de revisão manual; pin arrastável. Nenhuma parada é perdida — no pior caso fica no centroide com marcação vermelha. |
| ~~Semântica de `SITUACAO`/`TIPO_ENTREGA` não confirmada~~ | — | **Resolvido** — decodificado do fonte Delphi, ver §1.2.1. |
| Bases são snapshots históricos sem nada pendente | Importação vazia na demo | Modo `replanejar` (§1.2.1) replaneja um dia histórico. Modo `producao` fica pronto para o piloto real. |
| Volume baixo do cliente de entrega posterior (11–17 paradas/dia) | Ganho pouco impressionante | Demo principal na base de locação (46 paradas/dia, 50% coletas). Entrega posterior demonstrada no dia de pico, 2024-12-09 (215 paradas). |
| Preparação do OSRM demorada | Atraso | `osrm-extract` + `osrm-contract` do Centro-Oeste roda em background enquanto o resto é construído. |
| Baseline de locação é aproximado | Número contestável | Limitação declarada explicitamente na UI ao lado do valor. |

---

## 10. Critérios de sucesso

1. Importar um dia real de cada uma das duas bases e produzir rotas em menos de 30 segundos.
2. ≥ 70% das paradas geocodificadas com confiança `high` ou `medium`, sem intervenção.
3. Rotas visíveis no mapa, uma cor por caminhão, sequência numerada, ETA por parada.
4. Painel "Hoje vs Otimizado" com km, horas e R$/mês, sobre baseline real.
5. Romaneio PDF e link Waze funcionando por caminhão.
6. Pin arrastado persiste e é reaproveitado na execução seguinte.
7. `docker compose up` sobe a stack inteira do zero.
