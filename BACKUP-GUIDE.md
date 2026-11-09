# Artifact Keeper backup guide

Deployment checked on 2026-10-21:
`/home/seb/Developer/artifact-keeper/artifact-keeper`.

## Diagnosis

The API full backup failed because its artifact reader uses the global
`STORAGE_PATH`, while filesystem repositories store objects under their own
`repositories.storage_path`.

The first reported missing object actually exists at:

```text
/data/storage/python/47/21/4721f391ed90541fddacab5acf947aa0d3dc7d27b2e1e8eda2be8970586c3274
```

The backup reader attempts `/data/storage/47/21/...` instead. This verifies a
path mismatch for that object; it does not prove every other object is present.
Do not delete artifact records or move objects to work around the backup error.
The code fix is to read each artifact through its repository's resolved storage
backend and path. The archive format and restore logic must also preserve that
repository association, including identical keys in different repositories.
Changing `BACKUP_PATH` does not fix artifact reads.

## Does the built-in backup include remote cache data?

Not completely. The inspected implementation exports only `users`,
`repositories`, `artifacts`, `download_statistics`, `api_tokens`, `roles`,
`user_roles`, and `permission_grants`, then reads keys from `artifacts`.
It does not export `proxy_cache_artifacts` or enumerate its cached objects.
Some remote-related rows in `artifacts` may be included, but this is not a
complete remote-cache backup. This deployment has 5,027 proxy cache records
and approximately 2.5 GB under `/data/storage/proxy-cache`.

A PostgreSQL dump plus a copy of the entire artifact volume preserves both
remote cache metadata and cached bytes. It also preserves tables absent from
the built-in application export.

## Commands used to investigate

These are read-only diagnostics, apart from Docker creating/removing the
temporary inspection container. Run Docker with sufficient local permissions.

```bash
cd /home/seb/Developer/artifact-keeper/artifact-keeper
docker compose ls --format json
docker compose ps --format json
docker inspect artifact-keeper-backend --format '{{json .Mounts}}'
docker inspect artifact-keeper-caddy --format '{{json .NetworkSettings.Ports}}'
rg -n '^(HTTP_PORT|HTTPS_PORT|SITE_ADDRESS)=' .env
rg -n 'STORAGE_PATH|STORAGE_BACKEND|BACKUP_PATH|backup_storage|artifact_storage|image:|env_file:|S3_PREFIX|BACKUP_S3' docker-compose.yml

docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT storage_backend, storage_path, count(*) FROM repositories GROUP BY 1,2;"'
docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT r.key, count(*) FROM artifacts a JOIN repositories r ON r.id=a.repository_id GROUP BY r.key ORDER BY 2 DESC; SELECT count(*) AS cached_objects FROM proxy_cache_artifacts;"'

docker run --rm --network none \
  -v artifact-keeper_artifact_storage:/storage:ro alpine:3.24 sh -c \
  'find /storage -name 4721f391ed90541fddacab5acf947aa0d3dc7d27b2e1e8eda2be8970586c3274; du -sh /storage/*; find /storage/backups -type f'
```

## API backup commands

The API was called using Python urllib: first `POST /api/v1/admin/backups`
with `{"type":"full"}`, then `POST /api/v1/admin/backups/{id}/execute`.
The following reproduces those calls without putting the token in this file
or shell command history. Local certificate verification is disabled only
for this deployment's self-signed localhost certificate.

```bash
python3 - <<'PY'
import getpass
import json
import ssl
import urllib.request

token = getpass.getpass('Artifact Keeper API token: ')
base = 'https://localhost/api/v1/admin/backups'
opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=ssl._create_unverified_context()),
)
headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
def post(url, data):
    request = urllib.request.Request(
        url, data=json.dumps(data).encode(), headers=headers, method='POST')
    with opener.open(request, timeout=1800) as response:
        return json.load(response)
backup = post(base, {'type': 'full'})
print('Created:', json.dumps(backup), flush=True)
print('Executed:', json.dumps(post(base + '/' + backup['id'] + '/execute', {})))
PY
```

Actual result of the executed API run:

- ID: `0e773321-5838-4ac5-ba80-3b817c834ad7`
- Status: `failed`; 6,047 unreadable object reads; zero artifact objects included.
- Size: 1,174,906 bytes.
- Archive: `/data/storage/backups/2026/10/21/08c88f5d-b67b-45b4-957a-2b67a6202ae3.tar.gz`.

The archive exists in volume `artifact-keeper_artifact_storage`, not
`artifact-keeper_backup_storage`. A failed partial archive is not a full backup.
Do not enable `allow_partial_artifacts` as a repair.

## Complete backup, including cached data

The following procedure is provided for execution; it has not yet been run.
It stops the application while taking the database dump and copying volumes,
so database references and stored bytes remain consistent. PostgreSQL remains
running. Run the whole block with Bash, using a user authorized to access Docker.
Allow several GB of free space. The output includes credentials, tokens, and
private artifacts; the directory is created with private permissions.

This covers Artifact Keeper's database, artifact bytes including proxy cache,
plugins, shared configuration, separate backup volume, and local deployment
configuration. It does not snapshot OpenSearch indexes, Trivy cache, or the
optional Dependency-Track service's own database. Do not use this application
dump as a substitute for a Dependency-Track backup if that service is enabled.

```bash
set -euo pipefail
umask 077
cd /home/seb/Developer/artifact-keeper/artifact-keeper
backup_dir="/home/seb/Developer/artifact-keeper/backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_dir"

# Record the exact deployment before stopping it. These files contain secrets.
docker compose config > "$backup_dir/compose-resolved.yml"
docker inspect artifact-keeper-backend artifact-keeper-web > "$backup_dir/containers.json"
tar -czf "$backup_dir/deployment-config.tar.gz" docker-compose.yml .env docker

# Restart the application even if a subsequent backup command fails.
trap 'docker compose start backend web caddy' EXIT
docker compose stop caddy web backend

docker compose exec -T postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$backup_dir/database.dump"

for volume in artifact-keeper_artifact_storage artifact-keeper_backup_storage \
              artifact-keeper_plugins_storage artifact-keeper_shared_config; do
  docker run --rm --network none \
    -v "$volume:/source:ro" alpine:3.24 \
    tar -C /source -czf - . > "$backup_dir/$volume.tar.gz"
done

# Validate the dump catalogue and compressed archives.
docker compose exec -T postgres pg_restore --list \
  < "$backup_dir/database.dump" > "$backup_dir/database-contents.txt"
for archive in "$backup_dir"/*.tar.gz; do
  tar -tzf "$archive" > /dev/null
done
(cd "$backup_dir" && sha256sum database.dump *.tar.gz > SHA256SUMS)

docker compose start backend web caddy
trap - EXIT
docker compose ps
printf 'Backup saved to %s\n' "$backup_dir"
```

Copy the completed directory to another machine or backup storage. Checksums
and archive listings verify readability, not recovery: validate recovery in an
isolated deployment with the recorded image versions, restoring the database
and volume archives together before starting the backend. Do not restore over
the running deployment. The backend image currently uses `latest`; preserve
the image digest recorded in `containers.json` for reproducibility.

## Repository-path fix implemented locally

The local source now routes UI/API and scheduled artifact backups through the
repository storage registry. Version 2 archives use
`artifacts/repositories/<repository-uuid>/<storage-key>` and restore through
the repository's current configured storage location. Version 1 archives keep
legacy restore behavior. Both full and incremental artifact selection use the
repository-aware path; metadata-only backups still omit bytes.

This change fixes the 6,047 artifact-read failures. It does **not** add the
separate remote proxy cache catalog/content to built-in backups. The cache
limitation described above still applies. New version 2 archives require the
fixed backend for restore; do not restore them using an older backend.

The following additional check was run against live storage, read-only:

```bash
set -o pipefail
docker exec artifact-keeper-db psql -U registry -d artifact_registry -At \
  -c "SELECT r.storage_path || '/' || a.storage_key FROM artifacts a JOIN repositories r ON r.id=a.repository_id ORDER BY r.id, a.storage_key" |
  docker run --rm -i --network none \
    -v artifact-keeper_artifact_storage:/data/storage:ro alpine:3.24 sh -c '
      total=0
      missing=0
      while IFS= read -r path; do
        total=$((total+1))
        if ! cat "$path" > /dev/null 2>/dev/null; then
          missing=$((missing+1))
          printf "UNREADABLE: %s\n" "$path"
        fi
      done
      printf "Repository-resolved objects: %s; unreadable: %s\n" "$total" "$missing"
      test "$missing" -eq 0
    '
```

Result: **6,047 repository-resolved objects read, zero unreadable**.

### Build and deploy the local fix

These are deployment instructions, not commands already executed. Pulling
`latest` does not include uncommitted local source changes.

```bash
cd /home/seb/Developer/artifact-keeper
docker build -f docker/Dockerfile.backend \
  -t artifact-keeper-backend:backup-fix .

cd /home/seb/Developer/artifact-keeper/artifact-keeper
cat > compose.backup-fix.yml <<'YAML'
services:
  backend:
    image: artifact-keeper-backend:backup-fix
YAML

docker compose -f docker-compose.yml -f compose.backup-fix.yml \
  up -d --no-deps --pull never backend
docker compose -f docker-compose.yml -f compose.backup-fix.yml ps backend
docker compose logs --tail=100 backend
```

Use both `-f` arguments for subsequent Compose operations while using the local
image. Once deployed, create a new full backup in the UI or use the API commands
above. Previously failed archives still lack their bytes; updating the backend
does not repair those archives.

### Verification performed for the fix

- Backend library and executable pass `cargo check`.
- All 101 backup-service tests pass, including the new repository-aware full
  and incremental round-trip test, with database tests required (not skipped).
- The API failed-backup reporting regression passes.
- Four scheduled-backup claim/coordination tests pass.
- Live read-only storage verification: 6,047 objects readable, zero missing.
- Formatting and patch whitespace checks pass.

Tests used an isolated PostgreSQL container seeded with **schema only**, not
production data. No application services were stopped or replaced. Commands:

```bash
cd /home/seb/Developer/artifact-keeper

docker run -d --name ak-backup-fix-tests --network bridge \
  -p 127.0.0.1:35432:5432 \
  -e POSTGRES_USER=ak_test -e POSTGRES_PASSWORD=ak_test \
  -e POSTGRES_DB=ak_backup_test postgres:18-alpine

docker exec ak-backup-fix-tests pg_isready -U ak_test -d ak_backup_test

set -o pipefail
docker exec artifact-keeper-db pg_dump -U registry -d artifact_registry \
  --schema-only --no-owner --no-privileges |
  docker exec -i ak-backup-fix-tests psql -U ak_test -d ak_backup_test \
    -v ON_ERROR_STOP=1 > /tmp/ak-backup-test-schema.log

# The installed stable toolchain is Rust 1.99.0; the repo pins 1.98.0.
# Missing dependencies were downloaded into /tmp/ak-backup-cargo.
# protoc 31.1 was downloaded and extracted under /tmp/ak-backup-protoc.
cargo +stable fmt --all
CARGO_HOME=/tmp/ak-backup-cargo \
  PROTOC=/tmp/ak-backup-protoc/bin/protoc SQLX_OFFLINE=true \
  cargo +stable check -p artifact-keeper-backend --offline
CARGO_HOME=/tmp/ak-backup-cargo \
  PROTOC=/tmp/ak-backup-protoc/bin/protoc SQLX_OFFLINE=true \
  CARGO_PROFILE_DEV_DEBUG=0 CARGO_PROFILE_TEST_DEBUG=0 \
  cargo +stable test -p artifact-keeper-backend --lib \
    services::backup_service::tests::repository_ --no-run

# The binary path below is the actual artifact from this build.
# A different compiler/build configuration can change its hash suffix.
export DATABASE_URL=postgresql://ak_test:ak_test@127.0.0.1:35432/ak_backup_test
export AK_TESTS_REQUIRE_DB=1
target/debug/deps/artifact_keeper_backend-7b05a6f8d2163d99 \
  services::backup_service::tests:: --test-threads=1
target/debug/deps/artifact_keeper_backend-7b05a6f8d2163d99 \
  api::handlers::admin::tests::execute_reports_a_failed_run_as_the_backup_resource_not_a_500 \
  --exact --test-threads=1
target/debug/deps/artifact_keeper_backend-7b05a6f8d2163d99 \
  services::scheduler_service::tests::backup_ --test-threads=1

git diff --check
docker rm -f -v ak-backup-fix-tests
```
