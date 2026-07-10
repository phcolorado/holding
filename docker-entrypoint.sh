#!/bin/sh
# Aplica as migrações antes de subir o servidor.
set -e

python manage.py migrate --noinput

exec "$@"
