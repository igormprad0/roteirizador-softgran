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

| Perfil | O que otimizar realmente muda | Número medido |
|---|---|---|
| **Locação** (poliguindaste, capacidade 1) | **Cobertura** — quantas paradas a MESMA frota atende no dia | **25 otimizado vs. 15 na ordem de lançamento crua — +67%** |
| **Entrega posterior** | **Distância** percorrida | **29,6% menos km** (286,3 → 201,5 km) |

Cada perfil tem um argumento de venda diferente, e o número certo depende
da física: com capacidade 1, a rota de locação já é quase determinada por
ela (idas e vindas curtas ao depósito) — reordenar ganha pouco km (0,8%).
O que a otimização ganha de verdade é CABER mais paradas na mesma frota —
a ordem de lançamento crua, sem poder reordenar, só encaixa 15 das 25
paradas que o otimizador atende. Entrega posterior tem caminhões com mais
capacidade por viagem, então lá a distância evitada por uma rota melhor é
o número que importa. Os dois números vêm estruturados em
`comparison.optimized_stops`/`baseline_stops` (cobertura) e
`comparison.percent_km_saved` (distância) — a UI mostra os dois, para os
dois perfis, sempre lado a lado; ver "Limitações conhecidas" para a
história completa por trás do 0,8%.

## Subir do zero

```bash
powershell -File scripts/copy_fdb.ps1      # cópias dos .fdb (não usar os originais)
./scripts/prepare_osm.sh                   # 20-40 min: baixa e processa o mapa
docker compose up -d
docker compose run --rm api python -c "
from pathlib import Path
from api.app.geo.index_builder import build_street_index
print(build_street_index(Path('/srv/data/osm/regiao.osm.pbf'), Path('/srv/data/streets.db')))"
docker compose run --rm api python scripts/seed_demo.py
```

Abrir http://localhost:8000/app/

**Pré-requisito da demo: internet.** O Leaflet é vendorizado e o app funciona
offline, mas o mapa de fundo (tiles) vem de `tile.openstreetmap.org`. Sem
conectividade os pinos e as rotas continuam desenhando normalmente, só que
sobre um fundo escuro sem imagem de mapa.

## Testes

```bash
docker compose run --rm api pytest -v                          # tudo
docker compose run --rm api pytest -m "not erp and not stack"  # só unitários
```

`tests/test_e2e.py` (marcado `erp`, `stack`, `slow`) roda os dois dias reais
das duas bases contra a stack no ar — exige `docker compose up -d` com as
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
3. Painel **Hoje vs Otimizado**. **Liderar com a KPI "Paradas com a mesma
   frota": 25 otimizado vs. 15 na ordem atual.** Essa é a venda para
   locação — não o km. A física de capacidade 1 não deixa muita margem de
   km (0,8%, e o painel mostra isso também, sem esconder); o que a mesma
   frota ganha com otimização é caber 67% mais paradas no dia. Dizer que o
   baseline de locação é aproximado (o ERP não registra ordem) — isso
   constrói credibilidade.
4. **Entrega posterior, 09/12/2024** — o dia de pico, 215 paradas, para
   mostrar que escala. Depois, no dia de 13/08/2026, **liderar com a KPI de
   km**: 29,6% de economia (286,3 → 201,5 km) — aqui o argumento é
   distância, porque a frota tem caminhões de carga fracionada (mais
   capacidade por viagem), não capacidade 1.
5. Abrir um romaneio PDF e o link do Google Maps.

## Limitações conhecidas

- **Geocodificação automática**: 92% em alta/média confiança no dia de
  locação (04/08/2026, 35/38) e 78% no último dia de entrega posterior
  (13/08/2026, 14/18) — ambos medidos com plausibilidade geográfica (nenhum
  resultado fora da cidade informada pelo ERP), não por proximidade bruta.
  Deduplicado a endereços distintos, a entrega posterior cai para 11/15 =
  73%, apoiado majoritariamente em casamento aproximado (fuzzy), não exato —
  passa a régua de 70% com margem estreita, e um dia diferente pode empurrar
  para baixo dela. O restante entra na fila de revisão manual (arrastar o
  pino no mapa).
- **Cobertura de frota — achado operacional, não defeito.** No dia de
  locação medido, a frota configurada (`scripts/seed_demo.py`: 2 caminhões
  poliguindaste, capacidade 1, 8 + 6 viagens = 14 viagens/dia) atende 25 das
  38 paradas; 13 não cabem. Uma poliguindaste carrega uma caçamba por vez, e
  o dia real desse cliente excede o que essa frota consegue fazer. A
  ferramenta dizer "sua frota não cobre este dia" é um argumento de venda,
  não uma falha — e as paradas não atendidas aparecem destacadas na tela,
  nunca escondidas.
- **A economia medida no dia de locação é pequena: 0,8% (63,7 km → 63,1
  km, restrito às 15 das 25 paradas atendidas que o baseline consegue
  colocar em alguma viagem — ver próximo item).** Esse número tem uma
  história de três correções, cada uma no sentido oposto da anterior, e
  vale contar as três porque isso é evidência de que a medição está sendo
  levada a sério, não só o número final:
  - **81,8%** (bug original): o lado otimizado (23 paradas) era comparado
    contra um baseline sobre as 38 paradas do dia inteiro — a "economia"
    incluía trabalho que simplesmente não foi feito, não roteirização
    melhor.
  - **36,8%** (Task 13, primeira correção): restringiu os dois lados às
    mesmas paradas atendidas, mas com uma frota de teste menor (1 caminhão,
    capacidade 2, 6 viagens) — um placeholder, não a frota final; a própria
    Task 13 registrou isso como pendência explícita para esta task.
  - **0,6%** (esta task, primeira medição): com a frota final do seed (2
    caminhões, capacidade 1), mas com o baseline ainda particionando em
    blocos fixos de ~12 paradas, ignorando capacidade — fisicamente
    impossível para uma poliguindaste (uma caçamba por vez). Isso forçava o
    lado otimizado (que obedece capacidade, em idas e vindas curtas ao
    depósito) contra um baseline que a ignora (encadeando paradas sem nunca
    voltar) — o mesmo tipo de erro do 81,8%, só que no sentido inverso:
    subestimava a economia em vez de inflá-la.
  - **0,8%** (corrigido): o baseline agora respeita a MESMA frota e
    capacidade que o otimizador usa (`erp/locacao.py::baseline_order`),
    simulando a carga ao longo de cada viagem — uma entrega é pré-carregada
    no depósito e libera espaço ao ser entregue, uma coleta ocupa esse
    espaço ("sai cheio, volta cheio"), mas nunca mais de uma entrega a
    bordo ao mesmo tempo. Como o baseline preserva a ordem de lançamento
    estritamente (o otimizador pode reordenar; este baseline, por
    definição, não pode), ele só consegue encaixar 15 das 25 paradas
    atendidas nessa mesma frota de 14 viagens — o comparativo é restrito a
    essas 15, dos dois lados, para não repetir o erro de medir quantidades
    de trabalho diferentes.
  A conclusão honesta: para uma operação de poliguindaste com capacidade 1,
  a rota já é forçada, pela física, a ser uma sequência de idas e vindas
  curtas — e isso deixa pouca margem de ganho em quilometragem sobre
  qualquer ordem razoável. O valor real do otimizador aqui não é km: é
  **cobertura** (25 das 38 paradas do dia com esta frota, contra só 15
  numa despacho ingênuo que respeita a mesma ordem e capacidade) e janela de
  atendimento — não incluído em `percent_km_saved`. No dia de entrega
  posterior (13/08/2026) a economia é maior e mais representativa: 29,6%
  (286,3 km → 201,5 km), com baseline real (veículo e hora do ERP, não
  aproximado, sem essa restrição de cobertura).
- **O baseline de locação é aproximado.** O ERP não registra veículo nem
  ordem de rota para locação — o baseline reproduz um operador despachando
  a lista de lançamento de cima para baixo para a próxima viagem
  disponível da MESMA frota configurada, respeitando a capacidade de cada
  viagem (ver item da economia acima). Isso é sinalizado na tela
  (`comparison.approximate`) e no comparativo, nunca apresentado como se
  fosse a rota real executada. O baseline de entrega posterior é real: vem
  do veículo e horário gravados pelo ERP, por isso não tem essa
  aproximação nem a restrição de cobertura.
- Nada é gravado no Firebird do cliente. Escrever a rota de volta em
  `ENTREGA_PCAB` é fase 2.
- Perfil de rota é `car`. Restrição de caminhão (altura/peso) exige trocar o
  OSRM por Valhalla.
- **Nenhum navegador esteve disponível durante o desenvolvimento** — a UI
  foi verificada por conferência campo a campo da API e decodificação manual
  de uma polyline real, nunca por renderização de fato. O roteiro da demo
  acima é o primeiro teste real em navegador.
- O cache de geocodificação é permanente por endereço (`geocode_cache`, em
  `local.db`) e nunca se invalida sozinho quando o algoritmo de
  geocodificação muda — um ambiente de desenvolvimento de longa duração pode
  acumular resultados calculados por uma versão mais antiga do normalizador.
  Um clone novo (banco vazio) não tem esse problema; em caso de dúvida sobre
  se um número reflete o código atual, apagar as linhas com
  `is_manual = 0` de `geocode_cache` força recálculo.
