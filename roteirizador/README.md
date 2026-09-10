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

| Perfil / dia | Paradas atendidas | Distância | Ordenação do baseline | Cobertura com a mesma frota |
|---|---|---|---|---|
| **Locação** — 04/08/2026 | 25 de 38 | **19,0% menos km** (119,0 → 96,4) | ordem registrada | 25 otimizado vs. 25 |
| **Entrega posterior** — 13/08/2026 | 17 de 18 | **27,3% menos km** (277,1 → 201,5) | vizinho mais próximo | 17 vs. 17 |
| **Entrega posterior** — 09/12/2024 (pico) | 128 de 215 | **52,5% menos km** (189,3 → 89,8) | vizinho mais próximo | 128 vs. 128 |

### Contra qual ordenação o número é medido

A ordem que o ERP registra **não carrega informação espacial nenhuma**.
Dez embaralhamentos aleatórios das paradas de cada dia, pelo mesmo OSRM e
nas mesmas viagens, medem o mesmo que ela: pico 401,5 km contra 407,4 de
média aleatória (371,8–422,5), 13/08 278,6 contra 283,3 (266,6–292,1),
locação 119,0 contra 121,8 (113,9–129,2). Os três baselines caíam **dentro
da própria faixa aleatória** — a "economia" estava sendo medida contra um
sorteio, e o despachante de um cliente não é um gerador de números
aleatórios: ele dirige para a parada mais próxima.

Por isso o baseline hoje mede **cada viagem em duas ordens** — a
registrada/de lançamento e a de **vizinho mais próximo** das mesmas paradas
— e fica com a **mais curta**. A composição das viagens não muda: só a
ordem dentro de cada uma, com o mesmo OSRM, o mesmo tempo de serviço e a
mesma volta ao depósito dos dois lados. O guloso obedece à física de carga
(com capacidade 1, coletar antes de entregar não cabe) e, quando não fecha
a viagem, vale a ordem registrada. `comparison.baseline_method` diz qual
ordenação respondeu pela maior parte da quilometragem, e cada viagem em
`baseline.trips` traz o seu `method`.

O efeito é desigual **por construção**, e é isso que mostra que o método
está certo: o dia de pico tem ~16 paradas por viagem, onde a ordem domina,
e caiu de 77,6% para 52,5%; 13/08 e locação têm 4 e 1,8 paradas por viagem,
quase nada para errar na ordem, e praticamente não mudaram (27,7% → 27,3%,
19,0% → 19,0%) — nesses dois o ganho vem da **composição** das viagens, que
sempre foi legítima. Na locação nenhuma viagem trocou de ordem: com
capacidade 1 e pares entrega+coleta, a única troca possível é a que a
física proíbe.

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
  neste ERP é horário de digitação, não de despacho — mas a ordem
  registrada deixou de ser a régua: aquelas viagens são medidas também
  contra um despacho por vizinho mais próximo, que é o que um operador de
  verdade faz. **O valor do dia de pico é sobretudo escala** (215 paradas,
  166 endereços distintos, 13 s com cache quente); os 52,5% vêm depois
  disso, e agora são contra um despachante competente, não contra a ordem
  de digitação. Os 27,3% de 13/08 seguem sendo o número com o baseline
  mais próximo do registro real: ali a maior parte das paradas tem caminhão
  de verdade registrado.

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
Corrigido aquilo, o número ficou em `77,6%` — e caiu para **52,5%** quando
o baseline passou a ser medido também contra um despacho por vizinho mais
próximo: os 25 pontos de diferença eram a ordem de digitação do ERP, que
mede o mesmo que embaralhar as paradas.

`comparison` traz tudo estruturado: `percent_km_saved`,
`baseline_stops`/`optimized_stops` (cobertura), `baseline_method`
(`registrada` ou `vizinho_mais_proximo`), `approximate` e `note`; o
payload de `/api/optimize` traz ainda `baseline.trips` (cada viagem suposta,
com km e horas medidos) e `baseline.infeasible`.

### Sobre a "economia mensal"

`comparison.monthly_brl_saved` extrapola **um único dia** por 22 dias úteis
a **R$ 3,50/km** (`cost_per_km`, ajustável na chamada). Não é medição: é
uma conta de guardanapo em cima de uma amostra de um dia, e o custo por km
é um palpite razoável, não um dado do cliente. Nos dias acima dá R$
1.738,66 (locação), R$ 5.815,66 (13/08) e R$ 7.654,88 (pico) por mês.
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
   mesmas 25 paradas, com a mesma frota dos dois lados. A KPI "Rota atual"
   diz contra qual ordenação isso foi medido — neste dia, a registrada. A KPI "Paradas com a
   mesma frota" mostra 25 vs. 25 — um despacho razoável na ordem de
   lançamento cobre o mesmo dia; o que a otimização economiza aqui é
   distância, não cobertura. Dizer que o baseline é aproximado (o ERP não
   registra veículo nem ordem para locação) — isso constrói credibilidade, e
   a ressalva já aparece na tela sozinha.
4. **Entrega posterior, 13/08/2026** — **liderar com a KPI de km: 27,3%**
   (277,1 → 201,5), 17 das 18 paradas. É o número com o baseline mais
   próximo do registro real: a maior parte das paradas do dia tem caminhão
   de verdade registrado no ERP.
5. **Dia de pico, 09/12/2024 — este dia é sobre ESCALA.** 215 paradas
   importadas, 166 endereços distintos geocodificados, 128 atendidas em 11
   viagens, **13 s** com o cache quente. É a prova de que a ferramenta
   aguenta o pior dia do cliente, e é isso que se lidera aqui. **Aviso
   antes de clicar:** com o cache de geocodificação frio (primeira vez que
   este dia roda numa instalação nova) leva ~45s; se demorar, não travou.
   Só **depois da escala**, mencionar a economia: **52,5%** (189,3 → 89,8),
   agora medida contra um despachante que vai sempre à parada mais próxima
   — não contra a ordem de digitação do ERP, que era o que sustentava o
   77,6% anterior. A ressalva continua valendo e está na tela: 127 das 128
   paradas atendidas estavam numa caixa de despacho (`ENTREGA DUVIDOSA /
   BAIXA DV`), não num caminhão, então a composição das viagens desse dia é
   reconstruída.
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
  | Pico 09/12/2024 | 188/215 = **87%** | 166 |

  **Zero resultados de alta/média confiança apontando para fora da cidade
  informada, nos três dias.** O dia de pico é a amostra que sustenta o
  número: 166 endereços distintos, contra 15 e 31 dos dias da demo. O
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
  cache nas seguintes (medido nesta remedicao, com o `local.db` recriado
  do zero: 45,1s a frio e 12,9s a quente, no mesmo ambiente). Isto provavelmente explica os 45-49s "sob carga" que
  versões anteriores deste README atribuíam a contenção de CPU — foi
  observado justamente depois de o cache ter sido limpo, e um revisor numa
  segunda máquina, com cache quente, nunca reproduziu. O critério de 30s do
  `test_e2e.py` vale para o caminho com cache quente, que é o da demo; numa
  instalação recém-criada, aquecer o cache rodando o dia de pico uma vez
  antes de apresentar.
