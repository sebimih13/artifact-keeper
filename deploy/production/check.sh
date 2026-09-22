#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
docker compose --env-file .env -f compose.yml config --quiet
site=$(sed -n 's/^SITE_ADDRESS=//p' .env)
[[ -n "$site" ]] || { echo "SITE_ADDRESS is missing" >&2; exit 1; }
crt=certs/https/fullchain.pem
key=certs/https/privkey.pem
[[ -f "$crt" && -f "$key" ]] || {
  echo "Supply $crt and $key for $site before starting." >&2; exit 1;
}
openssl x509 -in "$crt" -noout -checkhost "$site"
openssl x509 -in "$crt" -noout -checkend 0
cert_public=$(openssl x509 -in "$crt" -pubkey -noout | openssl pkey -pubin -outform DER | sha256sum)
key_public=$(openssl pkey -in "$key" -passin pass: -pubout -outform DER | sha256sum)
[[ "$cert_public" == "$key_public" ]] || { echo "HTTPS key/certificate mismatch" >&2; exit 1; }
openssl verify -CAfile certs/postgres/postgres-ca.crt -verify_hostname postgres certs/postgres/postgres-server.crt
docker compose --env-file .env -f compose.yml run --rm --no-deps caddy \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
echo "Configuration and certificate checks passed. Client CA trust must be configured separately."
