#!/bin/sh
set -eu
# The backend owns its database but is not a PostgreSQL superuser.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=app_password="$POSTGRES_APP_PASSWORD" <<'SQL'
CREATE ROLE artifact_keeper LOGIN PASSWORD :'app_password';
ALTER DATABASE artifact_keeper OWNER TO artifact_keeper;
ALTER SCHEMA public OWNER TO artifact_keeper;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
SQL
