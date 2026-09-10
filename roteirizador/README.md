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
bases copiadas e o índice de ruas construído. Rode `scripts/seed_demo.py`
imediatamente antes, porque `tests/test_api.py` reconfigura a frota de
locação para seu próprio cenário de teste (1 caminhão, capacidade 2) e não
a restaura — ao rodar a suíte inteira de uma vez, isso derruba a asserção de
capacidade de `test_e2e.py` se ela rodar depois (os dois arquivos são os
únicos que apontam para o `local.db` real, em vez de um banco isolado por
teste). Não é um defeito do otimizador: rodando `test_e2e.py` sozinho logo
após o seed, os 12 testes passam.

## Roteiro da demo

1. **Locação, 04/08/2026** — importar. Mostrar os pinos por confiança de
   geocodificação e arrastar um vermelho para corrigir: a correção é permanente
   e vale para todos os pedidos daquele endereço.
2. Otimizar. Falar do caso poliguindaste: capacidade 1, várias viagens no dia,
   entregas e coletas alternadas — o caminhão nunca volta vazio. Com a frota
   configurada (2 caminhões, 14 viagens no total), a rota atende 25 das 38
   paradas do dia — as outras 13 não cabem na frota/janela do dia e aparecem
   destacadas, não escondidas.
3. Painel **Hoje vs Otimizado**: km, horas e R$/mês. Dizer que o baseline de
   locação é aproximado (o ERP não registra ordem) — isso constrói credibilidade.
4. **Entrega posterior, 09/12/2024** — o dia de pico, 215 paradas, para mostrar
   que escala.
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
- **A economia medida no dia de locação é pequena (0,6%, 97,8 km → 97,2
  km) — bem menor do que se poderia esperar.** O baseline de locação é a
  ordem de lançamento do ERP particionada em blocos de ~12 paradas (o ERP
  não registra veículo nem ordem real — ver item abaixo); com capacidade 1,
  a rota otimizada é necessariamente uma sequência de idas e vindas curtas
  ao depósito, e esse formato físico deixa pouca margem de ganho sobre um
  baseline que, mesmo em ordem arbitrária, encadeia várias paradas por
  bloco. Medições anteriores deste projeto, com uma frota de teste menor (1
  caminhão, capacidade 2, 6 viagens — antes de este seed final ser
  dimensionado), haviam chegado a 36,8%; com a frota atual, dimensionada
  para cobrir mais paradas reais, o número medido é este 0,6%. No dia de
  entrega posterior (13/08/2026) a economia é maior e mais representativa:
  29,6% (286,3 km → 201,5 km), com baseline real (veículo e hora do ERP, não
  aproximado). Uma correção conhecida evita a armadilha oposta: uma versão
  anterior deste cálculo comparava um lado otimizado de 23 paradas contra
  um baseline de 38, relatando 81,8% — o comparativo hoje mede sempre o
  mesmo conjunto de paradas nos dois lados.
- **O baseline de locação é aproximado.** O ERP não registra veículo nem
  ordem de rota para locação — o baseline reproduz um operador trabalhando
  de cima para baixo na lista de lançamento, particionada em blocos de ~12.
  Isso é sinalizado na tela (`comparison.approximate`) e no comparativo,
  nunca apresentado como se fosse a rota real executada. O baseline de
  entrega posterior é real: vem do veículo e horário gravados pelo ERP.
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
