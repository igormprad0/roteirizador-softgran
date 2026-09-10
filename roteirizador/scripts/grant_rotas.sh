#!/usr/bin/env bash
# Dá SELECT (e nada além) ao usuário `rotas` nas duas cópias de base do
# cliente. Precisa rodar UMA VEZ depois de cada `copy_fdb.ps1`: o arquivo
# .fdb vem do cliente sem nenhum privilégio para esse usuário, e é por isso
# que a API rodava como SYSDBA/masterkey -- com poder de escrita total sobre
# dados reais, por nada.
set -uo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1
for db in /db/locacao.fdb /db/entrega_posterior.fdb; do
  echo "== $db"
  # -e ecoa; cada GRANT é um statement independente e uma tabela ausente
  # naquela base só falha a própria linha (as duas bases não têm o mesmo
  # conjunto de tabelas).
  # `localhost:` na frente do caminho e obrigatorio: sem ele o isql abre
  # o .fdb em modo EMBUTIDO (o proprio isql vira o motor) e pede trava
  # exclusiva sobre o arquivo -- com o stack no ar isso falha com
  # "Database already opened with engine instance, incompatible with
  # current", justamente na hora natural de rodar o script: logo depois
  # do `docker compose up`. Com o prefixo, o isql conecta pelo SERVIDOR
  # que ja roda dentro do container -- quem detem a trava -- e o GRANT
  # passa com o stack de pe.
  docker compose exec -T firebird bash -lc \
    "while read -r sql; do [ -n \"\$sql\" ] && echo \"\$sql\" | /opt/firebird/bin/isql -u SYSDBA -p \${FIREBIRD_ROOT_PASSWORD:-masterkey} localhost:$db 2>&1 | grep -v '^$' ; done" \
    < scripts/grant_rotas.sql
done
echo "pronto -- confira com: docker compose exec api python -c \"from api.app.db.firebird import connect; from api.app.config import Profile; [print(p, connect(p).query('SELECT COUNT(*) AS N FROM CLIFOR')[0]) for p in Profile]\""
