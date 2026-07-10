#!/bin/sh
# Aplica as migrações antes de subir o servidor.
#
# Em PaaS com "release command" ou em deploys com múltiplas réplicas web,
# desabilite aqui (RUN_MIGRATIONS=0) e rode "python manage.py migrate" numa
# etapa de release única — evita migrações concorrentes entre réplicas.
set -e

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
    python manage.py migrate --noinput
else
    echo "RUN_MIGRATIONS=0 — migrações puladas (rode-as na etapa de release)."
fi

exec "$@"
