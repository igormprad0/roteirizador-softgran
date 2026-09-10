# Roteirizador — MVP

Roteirização de entregas e coletas a partir do ERP Softgran Empresarial.
Dois módulos atendidos pelo mesmo motor: **locação de equipamentos** (caçambas —
leva equipamento novo, traz o vencido) e **entrega posterior** (venda no balcão
com entrega agendada, mais devoluções).

## Stack

| Peça | Papel |
|---|---|
| OSRM | matriz de distâncias e geometria das rotas |
| VROOM | solver VRP — quem leva o quê, em que ordem |
| Firebird 3 | bases dos clientes, **acesso somente leitura** |
| FastAPI + SQLite | integração, geocodificação, estado próprio |
| Leaflet | mapa |

Geocodificação é feita por um índice de ruas construído do próprio `.pbf` do OSM,
sem depender de serviço externo. O ERP não tem nenhum campo de coordenada.

## Resultados medidos (dias reais das duas bases)

Medidos contra a API viva, com `local.db` recriado do zero (cache de
geocodificação vazio) e a frota de `scripts/seed_demo.py`. Três execuções
de cada dia deram o mesmo número.

| Perfil / dia | Paradas atendidas | Distância | Cobertura com a mesma frota | Baseline |
|---|---|---|---|---|
| **Locação** — 04/08/2026 | 25 de 38 | **19,0% menos km** (119,0 → 96,4) | 25 otimizado vs. 25 na ordem de lançamento | aproximado |
| **Entrega posterior** — 13/08/2026 | 17 de 18 | **27,7% menos km** (278,6 → 201,5) | 17 vs. 17 | parcialmente reconstruído |
| **Entrega posterior** — 09/12/2024 (pico) | 128 de 215 | **77,6% menos km** (401,5 → 89,8) | 128 vs. 128 | reconstruído — ver ressalva |

**Os três baselines carregam `approximate = true`, e isso não é detalhe de
rodapé: é a diferença entre um número e um número verificável.** A regra
que este projeto adotou depois de sete defeitos que faziam o número
melhorar sem quebrar teste nenhum é: *uma viagem de baseline que não
poderia ter sido executada nunca pode ser reportada como real*. Antes de
comparar, cada viagem suposta pelo baseline é conferida contra a maior
capacidade e o maior turno da frota configurada
(`service.viagens_inviaveis`); se alguma não passa, o comparativo sai
marcado. Nos três dias acima nenhuma viagem é inviável — a ressalva vem de
o baseline ser em parte RECONSTRUÍDO, não de ser impossível:

- **Locação**: o ERP não registra veículo nem ordem de rota. O baseline
  reproduz um despachante percorrendo a lista de lançamento e pegando o
  próximo pedido que cabe no caminhão já carregado.
- **Entrega posterior**: o ERP registra `ID_VEICULO`, mas metade daquela
  tabela não é caminhão — é caixa de despacho (`DEVOLUÇÃO/DV`,
  `PRÓPRIO/RETIRA`, `ENTREGA DUVIDOSA/BAIXA DV`, `DEP TRANSMITO/CIF`).
  Em 13/08 são 4 das 17 paradas atendidas; **no dia de pico são 127 das
  128**, quase todas sob `BAIXA DV`, que é baixa administrativa e não
  rota. Para essas, a viagem é reconstruída na ordem do campo `HORA` — que
  neste ERP é horário de digitação, não de despacho. **O 77,6% do dia de
  pico é, portanto, indicativo: mede uma boa rota contra uma ordem de
  digitação, não contra o que a operação de fato fez.** Os 27,7% de 13/08
  são o número mais defensável dos três: ali a maior parte das paradas tem
  caminhão registrado de verdade.

Números que este README já anunciou e que **não sobreviveram à medição**,
registrados aqui porque foi conferindo cada um deles que os defeitos
apareceram: `+67% de cobertura` na locação (25 vs. 15) era artefato de uma
regra que nenhum despachante segue — o baseline encerrava a viagem no
primeiro pedido *adjacente* que não coubesse, sem olhar o resto da fila, o
que com capacidade 1 prendia o número em `nº de viagens + 1` qualquer que
fosse o dado. Corrigido, os dois lados cobrem as **mesmas 25 paradas**, e o
ganho da locação aparece onde sempre esteve: em distância, 19,0%. E o
`77,0%` do dia de pico vinha comparado contra uma volta contínua de 125
paradas / 31,6 h de um caminhão que não existe, com `approximate = false`.

`comparison` traz tudo estruturado: `percent_km_saved`,
`baseline_stops`/`optimized_stops` (cobertura), `approximate` e `note`; o
payload de `/api/optimize` traz ainda `baseline.trips` (cada viagem suposta,
com km e horas medidos) e `baseline.infeasible`.

### Sobre a "economia mensal"

`comparison.monthly_brl_saved` extrapola **um único dia** por 22 dias úteis
a **R$ 3,50/km** (`cost_per_km`, ajustável na chamada). Não é medição: é
uma conta de guardanapo em cima de uma amostra de um dia, e o custo por km
é um palpite razoável, não um dado do cliente. Nos dias acima dá R$
1.738,66 (locação), R$ 5.938,24 (13/08) e R$ 23.994,89 (pico) por mês.
Apresentar como ordem de grandeza — e, de preferência, pedir o custo/km
real do cliente antes de mostrar.

## Subir do zero

```bash
powershell -File scripts/copy_fdb.ps1      # cópias dos .fdb (não usar os originais)
./scripts/prepare_osm.sh                   # 20-40 min: baixa e processa o mapa
docker compose up -d
docker compose run --rm api python -c "
from pathlib import Path
from api.app.geo.index_builder import build_street_index
print(build_street_index(Path('/srv/data/osm/regiao.osm.pbf'), Path('/srv/data/streets.db')))"
./scripts/grant_rotas.sh                   # SELECT (e só) para o usuário `rotas`
docker compose run --rm api python scripts/seed_demo.py
```

O `grant_rotas.sh` precisa rodar depois de cada `copy_fdb.ps1`: os `.fdb`
chegam do cliente sem privilégio nenhum para o usuário da aplicação, e é
por isso que a API rodava como SYSDBA. Firebird, OSRM e VROOM escutam só em
`127.0.0.1` — só a porta 8000 da aplicação fica exposta.

Abrir http://localhost:8000/app/ (ver "Limitações conhecidas" sobre a
internet exigida pelos ladrilhos do mapa).

## Testes

```bash
docker compose run --rm api pytest -v                          # tudo
docker compose run --rm api pytest -m "not erp and not stack"  # só unitários
```

`tests/test_e2e.py` (marcado `erp`, `stack`, `slow`) roda os três dias reais
das duas bases contra a stack no ar, e verifica por conta própria que cada
viagem suposta pelo baseline caberia na frota (capacidade e turno) -- sem
consultar o veredito do próprio portão de viabilidade, porque um portão que
só se autoconfirma não protege ninguém. Exige `docker compose up -d` com as
bases copiadas, o índice de ruas construído e `scripts/seed_demo.py` já
rodado. `tests/test_api.py` reconfigura a frota de locação repetidas vezes
para seus próprios cenários de teste, mas roda contra um `local.db`
temporário isolado (fixture `_local_db_isolado`), não contra o `local.db`
real do seed — a suíte inteira passa em qualquer ordem, sem depender de
quem rodou antes (verificado rodando `test_api.py`→`test_e2e.py` e
`test_e2e.py`→`test_api.py`, e a suíte inteira duas vezes em ordens
diferentes).

## Roteiro da demo

1. **Locação, 04/08/2026** — importar. Mostrar os pinos por confiança de
   geocodificação e arrastar um vermelho para corrigir: a correção é permanente
   e vale para todos os pedidos daquele endereço.
2. Otimizar. Falar do caso poliguindaste: capacidade 1, várias viagens no dia,
   entregas e coletas alternadas — o caminhão nunca volta vazio. Com a frota
   configurada (2 caminhões, 14 viagens no total), a rota atende 25 das 38
   paradas do dia — as outras 13 não cabem na frota/janela do dia e aparecem
   destacadas, não escondidas.
3. Painel **Hoje vs Otimizado**: **19,0% menos km** (119,0 → 96,4) sobre as
   mesmas 25 paradas, com a mesma frota dos dois lados. A KPI "Paradas com a
   mesma frota" mostra 25 vs. 25 — um despacho razoável na ordem de
   lançamento cobre o mesmo dia; o que a otimização economiza aqui é
   distância, não cobertura. Dizer que o baseline é aproximado (o ERP não
   registra veículo nem ordem para locação) — isso constrói credibilidade, e
   a ressalva já aparece na tela sozinha.
4. **Entrega posterior, 13/08/2026** — **liderar com a KPI de km: 27,7%**
   (278,6 → 201,5), 17 das 18 paradas. É o número mais defensável do
   conjunto: a maior parte das paradas do dia tem caminhão de verdade
   registrado no ERP.
5. **Dia de pico, 09/12/2024** — 215 paradas, para mostrar que escala.
   **Aviso antes de clicar:** com o cache de geocodificação frio (primeira
   vez que este dia roda numa instalação nova) leva ~45s; com o cache
   quente, 12-13s. Se demorar, não travou. E **ao mostrar o 77,6%, ler a
   ressalva junto**: 127 das 128 paradas atendidas estavam numa caixa de
   despacho do ERP (`ENTREGA DUVIDOSA / BAIXA DV`), não num caminhão, então
   o baseline desse dia é reconstruído a partir da ordem de digitação. É
   um número indicativo de escala, não a economia daquele dia.
6. Abrir um romaneio PDF (traz um link do Waze por parada e o link da rota
   inteira no Google Maps) e baixar o CSV.

## Limitações conhecidas

- **Geocodificação automática**, medida com plausibilidade geográfica
  (o ponto tem de cair dentro do raio da cidade que o ERP informou para
  aquela parada — não basta ter confiança alta) e com cache recriado do
  zero:

  | Dia | Alta/média plausível | Endereços distintos |
  |---|---|---|
  | Locação 04/08/2026 | 34/38 = **89%** | 31 |
  | Entrega 13/08/2026 | 14/18 = **78%** | 15 |
  | Pico 09/12/2024 | 188/215 = **87%** | 168 |

  **Zero resultados de alta/média confiança apontando para fora da cidade
  informada, nos três dias.** O dia de pico é a amostra que sustenta o
  número: 168 endereços distintos, contra 15 e 31 dos dias da demo. O
  restante entra na fila de revisão manual (arrastar o pino no mapa). O
  que sobra em `low` é lacuna de dado real: rua ausente do extrato OSM,
  propriedade rural sem nome de logradouro, condomínio não mapeado,
  referência de quilômetro em rodovia.

- **Todo baseline deste projeto é, em algum grau, reconstruído** — e é por
  isso que os três dias saem com `approximate = true`. O ERP de locação não
  registra veículo nem ordem; o de entrega posterior registra `ID_VEICULO`,
  mas metade dos valores é caixa de despacho e não caminhão (ver
  "Resultados medidos"). O portão de viabilidade
  (`service.viagens_inviaveis`) roda para os dois perfis e recusa apresentar
  como real qualquer viagem que estoure a maior capacidade ou o maior turno
  da frota configurada. Nos dias medidos ele não dispara — o que dispara é
  a marcação de reconstrução, que é mais fraca e mais honesta.

- **Cobertura de frota — achado operacional, não defeito.** No dia de
  locação medido, a frota configurada (`scripts/seed_demo.py`: 2 caminhões
  poliguindaste, capacidade 1, 8 + 6 viagens = 14 viagens/dia) atende 25 das
  38 paradas; 13 não cabem. Uma poliguindaste carrega uma caçamba por vez, e
  o dia real desse cliente excede o que essa frota consegue fazer. A
  ferramenta dizer "sua frota não cobre este dia" é um argumento de venda,
  não uma falha — e as paradas não atendidas aparecem destacadas na tela,
  nunca escondidas. Vale igual no dia de pico: 87 das 215 não cabem em 11
  viagens.

- **Histórico do número da locação**, que vale contar porque cada correção
  foi no sentido oposto da anterior e nenhuma delas quebrou teste:
  - **81,8%** — o lado otimizado (23 paradas) contra um baseline sobre as
    38 do dia: "economia" que incluía trabalho não feito.
  - **36,8%** — mesmos dois lados, mas com uma frota-placeholder menor.
  - **0,6% / 0,8%** — frota real, mas baseline particionando em blocos
    fixos de ~12 paradas, ignorando a capacidade 1 da poliguindaste: rota
    otimizada que obedece física contra baseline que a ignora.
  - **+67% de cobertura (25 vs. 15)** — o baseline passou a respeitar
    capacidade, mas encerrava a viagem no primeiro pedido *adjacente* que
    não coubesse. Com capacidade 1 isso fixava `baseline_stops` em
    `nº de viagens + 1` (15) independentemente dos dados: o tamanho da
    frota disfarçado de medição.
  - **19,0% de km, cobertura 25 vs. 25** (atual) — o despachante ingênuo
    pula o pedido que não cabe e leva o próximo que cabe, que é o que
    qualquer operação faz. Mesma ordem de lançamento, mesmas 14 viagens,
    mesma capacidade. Cobre o mesmo dia que o otimizador; roda 22,6 km a
    mais para fazer isso.

- Nada é gravado no Firebird do cliente. Escrever a rota de volta em
  `ENTREGA_PCAB` é fase 2. A aplicação conecta como `rotas`, com `SELECT` e
  nada mais (`scripts/grant_rotas.sh`), e o cliente Firebird recusa
  qualquer SQL que não comece com `SELECT`/`WITH`.
- Perfil de rota é `car`. Restrição de caminhão (altura/peso) exige trocar o
  OSRM por Valhalla.
- **Nenhum navegador esteve disponível durante o desenvolvimento** — a UI
  foi verificada por conferência campo a campo da API e decodificação manual
  de uma polyline real, nunca por renderização de fato. O roteiro da demo
  acima é o primeiro teste real em navegador.
- **Pré-requisito da demo: internet**, só para os ladrilhos do mapa
  (`tile.openstreetmap.org`). O Leaflet é vendorizado; sem conectividade os
  pinos e as rotas desenham normalmente sobre um fundo escuro sem imagem.
- O cache de geocodificação é permanente por endereço (`geocode_cache`, em
  `local.db`) e nunca se invalida sozinho quando o algoritmo de
  geocodificação muda — um ambiente de desenvolvimento de longa duração pode
  acumular resultados calculados por uma versão mais antiga do normalizador.
  Um clone novo (banco vazio) não tem esse problema; em caso de dúvida sobre
  se um número reflete o código atual, apagar as linhas com
  `is_manual = 0` de `geocode_cache` força recálculo. Todos os números deste
  README foram medidos com o `local.db` recriado do zero.
- **O dia de pico leva ~45s na primeira execução de uma instalação nova, e
  12-13s depois disso.** A diferença é o cache de geocodificação: são 215
  endereços resolvidos contra o índice de ruas na primeira vez, e leitura de
  cache nas seguintes (medido: 44,3s a frio, 12,7s e 12,9s a quente, no
  mesmo ambiente). Isto provavelmente explica os 45-49s "sob carga" que
  versões anteriores deste README atribuíam a contenção de CPU — foi
  observado justamente depois de o cache ter sido limpo, e um revisor numa
  segunda máquina, com cache quente, nunca reproduziu. O critério de 30s do
  `test_e2e.py` vale para o caminho com cache quente, que é o da demo; numa
  instalação recém-criada, aquecer o cache rodando o dia de pico uma vez
  antes de apresentar.
