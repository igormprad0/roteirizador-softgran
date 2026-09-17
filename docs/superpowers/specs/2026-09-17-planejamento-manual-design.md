# Planejamento manual de rotas — Design

**Data:** 2026-09-17
**Status:** Aprovado no desenho; spec para revisão
**Origem:** pedido do operador depois de testar o MVP em funcionamento

---

## 1. O que muda e por quê

O MVP otimiza o dia inteiro de uma vez: importa, distribui tudo na frota, devolve as rotas. Isso é bom para demonstrar e é de onde sai o número de economia. Mas não é como um despachante trabalha.

Na operação real ele sabe coisas que o sistema não sabe — que aquele cliente precisa receber de manhã, que aquele motorista conhece o bairro, que o cliente ligou pedindo retirada. Então ele quer escolher **quais paradas entram em qual caminhão** e deixar o otimizador cuidar do que ele não consegue fazer de cabeça: a ordem.

Três capacidades, nesta ordem de construção:

| | o que resolve | tamanho |
|---|---|---|
| **A. Correção de endereço** | cadastro errado no ERP obriga o motorista a adivinhar | pequena |
| **B. Retirada na locação** | o ERP não registra que o cliente ligou pedindo coleta | média |
| **C. Plano do dia** | montar rota a rota, por caminhão e horário | grande |

A e B são independentes entre si e do C. C se beneficia das duas, e por isso vem por último — o operador já vai poder usar as menores enquanto ela é construída.

**O modo automático continua existindo.** O botão que resolve o dia inteiro fica onde está; o manual é um caminho paralelo na mesma tela.

---

## 2. Feature A — correção de endereço

### O problema

Arrastar o pino conserta o mapa. Não conserta o papel: o romanejo continua imprimindo o endereço errado que veio do ERP, e é ele que o motorista lê. Um cliente com cadastro errado obriga a mesma correção toda semana.

### Desenho

Editar o endereço de uma parada grava uma **substituição ligada ao texto errado**, não à parada nem ao cliente. Na próxima importação, qualquer pedido que chegar com aquele mesmo endereço errado já vem corrigido.

Isso espelha como a correção de pino já funciona (`geocode_cache`, chaveado por `address_key`), e é o alcance certo: um endereço errado no cadastro produz o mesmo texto errado em todo pedido daquele cliente. Chavear por cliente erraria na locação, onde o endereço vem por locação e não do cadastro.

```sql
CREATE TABLE endereco_override (
  address_key TEXT PRIMARY KEY,   -- chave do endereço ERRADO, vindo do ERP
  logradouro TEXT, numero TEXT, bairro TEXT, cidade TEXT, uf TEXT,
  criado_em TEXT NOT NULL DEFAULT (datetime('now'))
);
```

`service.import_stops` aplica a substituição depois de montar o `Address` de cada parada e **antes** de geocodificar.

### Consequência que precisa ser dita na tela

O endereço corrigido gera uma `address_key` nova. Um pino que tinha sido arrastado para o endereço antigo **não se aplica mais** — o que está correto, porque é outro lugar, mas surpreende se não for avisado. A tela diz isso ao salvar a correção.

### Endpoints

```
PUT    /api/address/override   {address_key, logradouro, numero, bairro, cidade, uf}
                               -> {address_key_novo, lat, lon, confidence, source}
DELETE /api/address/override/{address_key}   -> volta a usar o do ERP
```

---

## 3. Feature B — retirada na locação

### O problema

Em locação o sistema **deduz** o que é coleta: replanejando um dia passado usa as devoluções que ocorreram; planejando o futuro usa a fila de vencidas. O ERP não tem campo dizendo "esta é retirada".

Os dois casos acontecem na operação:

- a fila de vencidas, que o sistema já traz;
- o cliente que **liga pedindo coleta** de uma locação que não está vencida — e que hoje não aparece em lugar nenhum.

### Desenho, duas peças

**B1. Marcar o que já está na tela.** Um botão por parada alternando entrega ⇄ retirada, sobrepondo o que foi deduzido. Gravado pelo identificador da parada, então sobrevive a reimportar.

```sql
CREATE TABLE parada_override (
  external_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('delivery','pickup')),
  criado_em TEXT NOT NULL DEFAULT (datetime('now'))
);
```

**B2. Puxar uma locação em aberto.** Busca por nome, documento ou endereço sobre tudo que está em locação (`SITUACAO = 1`), não só as vencidas. O que for escolhido entra no dia como coleta.

```sql
CREATE TABLE parada_extra (
  profile TEXT NOT NULL, target_date TEXT NOT NULL, external_id TEXT NOT NULL,
  criado_em TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (profile, target_date, external_id)
);
```

A escolha de desenho que importa: a parada puxada **vira parte do dia**, gravada, e `import_stops` passa a devolver as paradas da data **mais** as extras daquele dia. Nada a jusante muda — o modo automático, o comparativo, o romanejo e o plano manual tratam uma parada puxada exatamente como qualquer outra. A alternativa (a tela guardar as extras só na memória) obrigaria todo endpoint a receber paradas soltas, e espalharia o conceito por todo o sistema.

Exige um método novo no `StopSource`: materializar paradas por identificador, não por data.

### Endpoints

```
PUT    /api/stops/{external_id}/kind     {kind}          -> sobrepõe entrega/retirada
DELETE /api/stops/{external_id}/kind                     -> volta ao deduzido
GET    /api/locacao/abertas?q=<texto>&limit=20           -> busca locações em aberto
POST   /api/stops/extra   {profile, date, external_id, kind}  -> puxa para o dia
DELETE /api/stops/extra   {profile, date, external_id}        -> tira do dia
```

---

## 4. Feature C — plano do dia

### Fluxo

1. Importar (como hoje) → tudo cai no **repositório** de paradas disponíveis
2. Selecionar paradas (caixa na lista ou clique no pino)
3. Escolher **caminhão** e **horário de saída**
4. **Montar rota** → o otimizador ordena aquela seleção
5. A rota entra no **plano**; suas paradas saem do repositório
6. Repetir para o próximo caminhão, ou para o mesmo saindo mais tarde
7. Exportar o plano: um romanejo por rota, um CSV do dia

### A divisão automática em viagens

Um poliguindaste carrega uma caçamba. Uma "rota" de 10 paradas é fisicamente impossível como viagem única.

Então: a seleção é atribuída ao **caminhão**, e o sistema a divide em quantas viagens a capacidade exigir, encadeadas a partir do horário informado. O operador pensa em "o que esse caminhão faz hoje", não em viagem por viagem.

Limite: o número de viagens configurado para aquele caminhão. Se a seleção não couber nem assim, as paradas que sobrarem voltam ao repositório **nomeadas**, com o motivo. Nunca sumir em silêncio — é a regra que governa este projeto.

### O comparativo fica melhor, não pior

Em modo automático o comparativo carrega ressalvas: a composição das viagens do baseline é reconstruída, e a ordem registrada no ERP muitas vezes não carrega informação espacial.

No modo manual isso desaparece. As paradas são **as mesmas dos dois lados por construção** — o operador escolheu — e o caminhão é o mesmo. A única diferença é a ordem: a que ele selecionou contra a que o otimizador achou. É a comparação mais limpa que este sistema consegue produzir, e vale exibi-la por rota.

### Persistência

O plano **vive enquanto a aba estiver aberta**. Cada rota montada é salva no servidor como uma execução (`route_run`, que já existe), então o romanejo e o CSV funcionam; o que não sobrevive a fechar o navegador é a *montagem* — a lista de quais rotas compõem o dia.

Decisão consciente: guardar o plano em disco exigiria decidir o que fazer quando os dados do ERP mudam no meio da montagem, e isso não se paga num MVP. Se o operador reclamar de perder a montagem, vira a próxima feature.

### Endpoint

```
POST /api/route/build
  {profile, date, mode, stop_ids[], vehicle_id, start_s, cost_per_km}
  -> {run_id, routes[], unassigned[], totals, comparison}
```

`routes[]` porque uma seleção pode virar várias viagens do mesmo caminhão. `unassigned[]` traz o que não coube, com motivo.

---

## 5. Riscos

| risco | mitigação |
|---|---|
| Seleção grande demais para o caminhão vira frustração silenciosa | O que não couber volta ao repositório nomeado e com motivo, e a tela diz quantas |
| Correção de endereço invalida um pino já arrastado | A tela avisa ao salvar; é o comportamento correto, só precisa ser visível |
| Parada puxada manualmente esquecida no dia seguinte | `parada_extra` é por data; um dia novo começa limpo |
| Plano perdido ao fechar a aba | Declarado na tela antes de o operador investir meia hora montando |
| Modo manual e automático divergirem em regra de negócio | Ambos passam por `service`, não por caminhos paralelos; a divisão em viagens reusa `expand_trips` |

---

## 6. Critérios de sucesso

1. Corrigir um endereço errado uma vez, reimportar, e ele vir certo — no mapa e no romanejo.
2. Marcar uma entrega como retirada, reimportar, e a marcação continuar.
3. Achar pelo nome uma locação em aberto não vencida, puxá-la para o dia, e ela aparecer como coleta.
4. Selecionar paradas, mandar para um caminhão às 7h, e receber as viagens encadeadas com a ordem otimizada.
5. Montar duas rotas no mesmo dia sem que nenhuma parada apareça nas duas.
6. Desfazer uma rota e ver suas paradas voltarem ao repositório.
7. Exportar o plano inteiro: um romanejo por rota e um CSV com tudo.
8. Nenhuma parada selecionada desaparecer sem aparecer em `unassigned` com motivo.

---

## 7. Fora de escopo

Escrever qualquer coisa no Firebird do cliente · plano persistido entre sessões · reatribuir uma parada de uma rota montada para outra sem desfazer · app do motorista · trânsito.
