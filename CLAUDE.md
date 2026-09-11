# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## O que é

Roteirizador de entregas e coletas acoplado ao ERP Softgran Empresarial (Delphi + Firebird 3). Atende dois clientes reais de Dourados-MS com o mesmo motor: locação de caçambas (poliguindaste, capacidade 1, sai cheio e volta cheio) e entrega posterior (venda no balcão com entrega agendada, mais devoluções).

Tudo vive em `roteirizador/`. `docs/superpowers/` guarda o spec e o plano de implementação.

## Comandos

**Todo teste roda dentro do container.** Não existe Python no host Windows para este projeto.

```bash
cd roteirizador
docker compose up -d                                   # osrm, vroom, firebird, api
docker compose run --rm api pytest -q                  # suíte completa (282)
docker compose run --rm api pytest tests/test_geocoder.py -v
docker compose run --rm api pytest -k nome_do_teste -v # um teste
docker compose run --rm api pytest -v -m stack         # só os que exigem OSRM/VROOM
docker compose run --rm api pytest -v -m erp           # só os que leem as bases
```

Marcadores em `pytest.ini`: `stack` (precisa de OSRM/VROOM), `erp` (precisa do Firebird com as cópias), `slow`.

UI em http://localhost:8000/app/ depois de `docker compose up -d`.

### Duas armadilhas de ambiente que custam tempo

- **`export MSYS_NO_PATHCONV=1`** antes de qualquer `docker compose run` que passe caminho absoluto de container no Git Bash. Sem isso `/srv/data/x.py` vira `/srv/C:/Program Files/Git/srv/data/x.py`. Já quebrou o build do grafo OSM uma vez.
- **`docker compose restart api`** depois de editar código. O container tem bind mount mas o processo já importou os módulos; sem restart você testa a versão antiga e se engana.

### Preparo (uma vez, e não está no repositório)

`data/` é gitignored e **não vem no clone**. Numa máquina nova:

1. `scripts/copy_fdb.ps1` — copia os dois `.fdb` dos clientes para `data/fdb/` com nome ASCII (o nome com cedilha quebra `isql` e o mount). As bases têm 4,8 GB e são transferidas por fora do git.
2. `scripts/prepare_osm.sh` — baixa o extrato do Centro-Oeste, recorta MS e roda `osrm-extract` + `osrm-contract`. 20 a 40 minutos.
3. `docker compose run --rm api python -c "from pathlib import Path; from api.app.geo.index_builder import build_street_index; print(build_street_index(Path('/srv/data/osm/regiao.osm.pbf'), Path('/srv/data/streets.db')))"` — índice de ruas.
4. `docker compose run --rm api python scripts/seed_demo.py` — frota e depósito.
5. `scripts/grant_rotas.sh` — dá SELECT ao usuário `rotas` (pare a stack antes; o script usa o servidor, não o motor embutido).

## Arquitetura

Cinco containers: **OSRM** (matriz e geometria, grafo de MS), **VROOM** (solver VRP), **Firebird** (cópias das bases dos clientes), **api** (FastAPI, orquestra e serve o front), e o front estático em `web/`.

Fluxo: `service.optimize()` → `StopSource.fetch()` lê o ERP → `Geocoder.geocode()` inventa as coordenadas → `Optimizer.solve()` monta o payload VROOM e chama o solver → `measure_baseline()` mede a rota que a operação teria feito → `compare()` produz o número.

### Três protocolos, e a fronteira se sustenta

- `erp/base.py::StopSource` — ERP → `list[Stop]`. Implementado por `locacao.py` e `entrega_posterior.py`. Adicionar um terceiro módulo do ERP é escrever um `StopSource` novo e nada mais.
- `geo/geocoder.py::Geocoder` — `Address` → `GeoResult`.
- `routing/optimizer.py::Optimizer` — paradas + frota → `Solution`.

`geo/` nunca importa `erp/`. `routing/` nunca importa `erp/` nem `geo/`. `service.py` é o único lugar onde os três se encontram. `models.py` não importa nada do projeto.

## Restrições de domínio que você precisa saber antes de mexer

**O ERP não tem nenhuma coordenada.** 558 tabelas, zero colunas geográficas. Todas as coordenadas do sistema são inventadas pelo geocoder, construído a partir do mesmo `.pbf` que alimenta o OSRM. Não há serviço externo de geocodificação. Uma coordenada plausível e errada é pior que uma falha óbvia — ela reordena a rota em silêncio.

**Acesso ao ERP é somente leitura, sempre.** `db/firebird.py` recusa qualquer SQL que não comece com `SELECT` ou `WITH`, e a conexão usa o usuário `rotas`, que no banco só tem `SELECT` (`RDB$USER_PRIVILEGES`). Nunca escreva no Firebird do cliente. Trabalhe sempre nas cópias em `data/fdb/`.

**Coordenadas são `(lon, lat)` em toda fronteira interna.** Só dois lugares invertem: `web/app.js` (Leaflet usa `lat,lng`) e `export/maps_link.py` (Google e Waze usam `lat,lon`). Dourados fica em lon −54,8 e lat −22,2 — ambos negativos e de magnitude parecida, então uma troca produz um ponto plausível e errado.

**`charset="WIN1252"`** na conexão Firebird é obrigatório; sem ele a acentuação volta corrompida.

**Tipos FLOAT.** `CLIFOR.ID`, `ID_CLIENTE`, `ID_SEQUENCIA`, `ID_CONTROLE` são `FLOAT` no schema. Sem `int()` na fronteira o `external_id` sai como `EP:162886.0`.

### A regra que governa qualquer número apresentado

> **Uma viagem de baseline que não poderia ter sido executada nunca pode ser reportada como real.**

Oito defeitos neste projeto fizeram algum número parecer melhor (ou pior) do que era, e **nenhum quebrou um teste**. Entre eles: geocodificação a 97% contando endereços resolvidos a 145 km na cidade errada; `optimized_km` que ficaria em zero para sempre; economia de 81,8% comparando 23 paradas otimizadas contra 38 no baseline; um baseline de 12 paradas numa volta contínua para um caminhão que carrega uma; e um percentual medido contra uma ordem estatisticamente indistinguível do acaso.

Consequências práticas no código:

- `service.optimize()` chama `viagens_inviaveis()` e força `approximate=True` se alguma viagem do baseline estoura capacidade ou turno. Há teste que falha se essa chamada sumir.
- O baseline mede **as mesmas paradas** que a solução atendeu, não todas as importadas.
- O baseline usa a melhor entre a ordem registrada e um despachante de vizinho mais próximo — `comparison.baseline_method` diz qual venceu.
- Ao criar qualquer número novo para o usuário, pergunte o que ele mede, não se um teste o afirma. Se parecer bom demais, provavelmente está medindo outra coisa.

### `ImportMode`

As bases entregues são snapshots históricos: `ENTREGA_PCAB.SITUACAO = 1` (pendente) tem **zero linhas**. Por isso existem dois modos.

- `PRODUCAO` — o que está de fato pendente. Em entrega posterior devolve vazio nestas bases; em locação as coletas são o backlog de vencidas.
- `REPLANEJAR` — replaneja um dia histórico (`SITUACAO <> 3`). É o modo da demo, e é o que dá o baseline de graça, porque o dia histórico traz veículo e hora reais.

Em locação as coletas mudam por modo: em `REPLANEJAR` são as devoluções que de fato ocorreram naquele dia (`DATA_DEVOLUCAO = D`); em `PRODUCAO` são as locações vencidas ainda abertas.

### Códigos do ERP (decodificados do fonte Delphi, não adivinhados)

- `ENTREGA_CAB.SITUACAO`: 2 = aberto com saldo, 3 = totalmente entregue.
- `ENTREGA_PCAB.SITUACAO`: 1 = agendado, 2 = executado, 3 = cancelado.
- `ENTREGA_CAB.TIPO_ENTREGA`: 1 data específica, **2 = cliente retira no balcão (excluir sempre)**, 3 aguardando autorização, 9 outros.
- `LOCACAO_PRODUTO.SITUACAO`: 1 = em locação, 2 = devolvido.
- `VEICULO` **não é frota.** Contém caixinhas de despacho: `DEVOLUÇÃO/DV`, `PRÓPRIO/RETIRA`, `ENTREGA DUVIDOSA/BAIXA DV`, `DEP TRANSMITO/CIF`. Capacidades são todas `NULL`. A frota real é configurada no roteirizador e persistida no SQLite.

## Estado próprio

SQLite em `data/local.db`: cache de geocodificação (com pins manuais que nunca são sobrescritos automaticamente), frota, depósito e execuções salvas. Nada é escrito no Firebird do cliente.

**Ao medir taxa de geocodificação, use um `LocalStore` descartável.** O cache compartilhado replica resultados de versões antigas do algoritmo e mascara qualquer mudança.

## Testes

Convenções que valem a pena manter:

- Os endereços nos testes são amostras reais das bases, incluindo os degenerados (`"HECTARES"`, `"JM EVENTO - ROD DDOS A ITAPORA"`). Não os "limpe".
- `test_geocode_e_deterministico_entre_processos` abre um segundo interpretador com `PYTHONHASHSEED` diferente. Determinismo entre processos não é detectável dentro de um só.
- Testes que guardam invariante importante devem ser provados por mutação: aplique o bug, veja falhar, reverta, veja passar. Três testes deste projeto existem porque a correção anterior podia ser apagada com a suíte verde.
