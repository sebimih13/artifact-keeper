# Artifact Keeper: PostgreSQL and Docker-volume backups (GNU Make + Bash).
#
# Requirements: Docker Engine, a running Artifact Keeper PostgreSQL container,
# Bash, GNU tar/coreutils, and GNU Make. No downloads or helper images needed:
# the already-local PostgreSQL image is reused for volume archive operations.
#
# Examples:
#   make backup
#   make restore RESTORE_FILE=/path/to/artifact_registry_....dump CONFIRM=YES
#   make backup-all
#   make restore-all RESTORE_FILE=/path/to/artifact_keeper_....tar.gz CONFIRM=YES
#   make clean
#
# IMPORTANT: Backups contain credentials, TLS private keys, and potentially
# private artifacts. Keep them off-host too and test restoration regularly.

SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c
.ONESHELL:
.DEFAULT_GOAL := help

MAKEFILE_DIR := $(abspath $(dir $(firstword $(MAKEFILE_LIST))))
POSTGRES_CONTAINER ?= artifact-keeper-db
BACKUP_DIR ?= $(MAKEFILE_DIR)/artifact-keeper-backups
BACKUP_DIR_ABS := $(abspath $(BACKUP_DIR))
TIMESTAMP := $(shell date +%Y-%m-%d_%H-%M-%S)
BACKUP_FILE ?= $(BACKUP_DIR_ABS)/artifact_registry_$(TIMESTAMP).dump
FULL_BACKUP_FILE ?= $(BACKUP_DIR_ABS)/artifact_keeper_$(TIMESTAMP).tar.gz
RESTORE_FILE ?=
CONFIRM ?= NO
BACKUPS_TO_KEEP ?= 10
STOP_TIMEOUT ?= 60  # Seconds for application containers to shut down cleanly

# Only volumes containing persistent application state. postgres_data is NOT
# copied raw: its artifact_registry database is backed up with pg_dump instead.
# trivy_cache and scan_workspace belong to the (currently disabled) scanner;
# remove them from the list for smaller archives. Missing optional volumes are
# skipped during backup. Extend for your stack.
DATA_VOLUMES ?= artifact_storage backup_storage plugins_storage trivy_cache opensearch_data scan_workspace
REQUIRED_VOLUMES ?= artifact_storage

.PHONY: help backup restore backup-all restore-all clean verify

# Functions run within a single recipe shell (because .ONESHELL is enabled).
# NOTE: every shell '$' is escaped as '$$' so Make doesn't expand it.
define HELPERS
umask 077
fail() { echo "ERROR: $$*" >&2; exit 1; }
check_db() {
  command -v docker >/dev/null || fail 'Docker CLI not found'
  docker info >/dev/null 2>&1 || fail 'Docker daemon is unavailable'
  docker inspect '$(POSTGRES_CONTAINER)' >/dev/null 2>&1 || fail 'PostgreSQL container does not exist'
  [[ "$$(docker inspect -f '{{.State.Running}}' '$(POSTGRES_CONTAINER)')" == true ]] || fail 'PostgreSQL container is not running'
}
check_file() { [[ -f "$$1" && -s "$$1" ]] || fail "File missing or empty: $$1"; }
confirm_restore() {
  [[ '$(CONFIRM)' == YES ]] || fail 'Destructive restore requires CONFIRM=YES'
  [[ -n '$(RESTORE_FILE)' ]] || fail 'Set RESTORE_FILE=/path/to/backup'
  check_file '$(RESTORE_FILE)'
}
project_name() {
  local project
  project="$$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' '$(POSTGRES_CONTAINER)')"
  [[ -n "$$project" && "$$project" != '<no value>' ]] || fail 'PostgreSQL container has no Docker Compose project label'
  printf '%s\n' "$$project"
}
postgres_image() {
  docker inspect -f '{{.Image}}' '$(POSTGRES_CONTAINER)'
}
# Resolve actual Docker volume name using Compose's labels, with a fallback for
# a default project-prefixed volume. Never silently create a volume on restore.
volume_name() {
  local project="$$1" logical="$$2" candidate
  local -a matches=()
  mapfile -t matches < <(docker volume ls -q \
    --filter "label=com.docker.compose.project=$$project" \
    --filter "label=com.docker.compose.volume=$$logical")
  (( $${#matches[@]} < 2 )) || fail "Multiple Docker volumes match $$project / $$logical"
  if (( $${#matches[@]} == 1 )); then
    printf '%s\n' "$${matches[0]}"
    return 0
  fi
  candidate="$${project}_$${logical}"
  if docker volume inspect "$$candidate" >/dev/null 2>&1; then
    printf '%s\n' "$$candidate"
    return 0
  fi
  return 1
}
validate_dump() {
  check_file "$$1"
  docker exec -i '$(POSTGRES_CONTAINER)' pg_restore --list < "$$1" > /dev/null \
    || fail "Invalid or incompatible PostgreSQL custom-format dump: $$1"
}
dump_database() {
  local destination="$$1"
  # No -t: pg_dump produces binary output. Database credentials come from the
  # container's environment rather than being hardcoded in this Makefile.
  docker exec '$(POSTGRES_CONTAINER)' /bin/sh -ec '
    : "$${POSTGRES_USER:?POSTGRES_USER is not set}"
    : "$${POSTGRES_DB:?POSTGRES_DB is not set}"
    export PGPASSWORD="$${POSTGRES_PASSWORD:-}"
    exec pg_dump --format=custom --compress=6 --no-owner --no-acl \
      --no-password --username="$$POSTGRES_USER" --dbname="$$POSTGRES_DB"
  ' > "$$destination"
  validate_dump "$$destination"
}
restore_database() {
  local source="$$1"
  validate_dump "$$source" # BEFORE dropping the existing database
  # Every non-Postgres Compose service must already be stopped. A failure is
  # deliberately fatal; do not bring the application back up automatically.
  docker exec '$(POSTGRES_CONTAINER)' /bin/sh -ec '
    : "$${POSTGRES_USER:?POSTGRES_USER is not set}"
    : "$${POSTGRES_DB:?POSTGRES_DB is not set}"
    [ "$${POSTGRES_DB}" != postgres ] || { echo "Refusing to replace maintenance database" >&2; exit 1; }
    export PGPASSWORD="$${POSTGRES_PASSWORD:-}"
    dropdb --if-exists --force --no-password --username="$$POSTGRES_USER" "$$POSTGRES_DB"
    createdb --no-password --username="$$POSTGRES_USER" --owner="$$POSTGRES_USER" "$$POSTGRES_DB"
  '
  docker exec -i '$(POSTGRES_CONTAINER)' /bin/sh -ec '
    export PGPASSWORD="$${POSTGRES_PASSWORD:-}"
    exec pg_restore --exit-on-error --single-transaction --no-owner --no-acl \
      --no-password --username="$$POSTGRES_USER" --dbname="$$POSTGRES_DB"
  ' < "$$source"
}
# Stop only running containers belonging to the same Compose project, except
# PostgreSQL. Track exactly what we stopped, so previously stopped services
# remain stopped. This quiesces Artifact Keeper's artifact-storage writers.
quiesce_project() {
  local project="$$1" db_id id
  db_id="$$(docker inspect -f '{{.Id}}' '$(POSTGRES_CONTAINER)')"
  local -a running=()
  mapfile -t running < <(docker ps --no-trunc -q \
    --filter "label=com.docker.compose.project=$$project")
  stopped=()
  for id in "$${running[@]}"; do
    [[ "$$id" == "$$db_id" ]] || stopped+=("$$id")
  done
  if (( $${#stopped[@]} )); then
    echo "Stopping $${#stopped[@]} running application container(s) for consistent backup/restore..."
    docker stop --time $(STOP_TIMEOUT) "$${stopped[@]}" > /dev/null
  fi
}
start_stopped() {
  if (( $${#stopped[@]} )); then
    echo "Restarting $${#stopped[@]} previously running container(s)..."
    docker start "$${stopped[@]}" > /dev/null
  fi
}
prune_backups() {
  local keep='$(BACKUPS_TO_KEEP)' i
  [[ "$$keep" =~ ^[1-9][0-9]*$$ ]] || fail 'BACKUPS_TO_KEEP must be a positive integer'
  local -a files=()
  mapfile -d '' -t files < <(find '$(BACKUP_DIR_ABS)' -maxdepth 1 -type f \
    \( -name 'artifact_registry_*.dump' -o -name 'artifact_keeper_*.tar.gz' \) \
    -printf '%T@ %p\0' | sort -zrn | cut -z -d ' ' -f 2-)
  for (( i=keep; i<$${#files[@]}; i++ )); do
    echo "Removing old backup: $${files[i]}"
    rm -f -- "$${files[i]}"
  done
}
endef

backup:
	@$(HELPERS)
	check_db
	mkdir -p -- '$(BACKUP_DIR_ABS)'
	out='$(BACKUP_FILE)'
	mkdir -p -- "$$(dirname "$$out")"
	[[ ! -e "$$out" ]] || fail "Backup already exists: $$out"
	tmp="$$out.partial"
	trap 'rm -f -- "$$tmp"' EXIT
	echo 'Backing up PostgreSQL (online snapshot)...'
	dump_database "$$tmp"
	mv -- "$$tmp" "$$out"
	trap - EXIT
	prune_backups
	echo "PostgreSQL backup: $$out"

restore:
	@$(HELPERS)
	check_db
	confirm_restore
	validate_dump '$(RESTORE_FILE)'
	project="$$(project_name)"
	stopped=()
	completed=0
	cleanup() {
	  status=$$?
	  trap - EXIT
	  if (( status == 0 && completed == 1 )); then
	    start_stopped || status=1
	  else
	    echo 'Restore failed: application containers are intentionally left stopped.' >&2
	  fi
	  exit "$$status"
	}
	trap cleanup EXIT
	quiesce_project "$$project"
	echo 'Replacing PostgreSQL database...'
	restore_database '$(RESTORE_FILE)'
	completed=1
	echo 'PostgreSQL restored successfully.'

backup-all:
	@$(HELPERS)
	check_db
	project="$$(project_name)"
	image="$$(postgres_image)"
	mkdir -p -- '$(BACKUP_DIR_ABS)'
	archive='$(FULL_BACKUP_FILE)'
	mkdir -p -- "$$(dirname "$$archive")"
	[[ ! -e "$$archive" ]] || fail "Backup already exists: $$archive"
	stage="$$(mktemp -d '$(BACKUP_DIR_ABS)/.backup-stage.XXXXXXXX')"
	partial="$$archive.partial"
	stopped=()
	cleanup() {
	  status=$$?
	  trap - EXIT
	  # On backup (unlike restore), always try to resume application services.
	  start_stopped || status=1
	  rm -rf -- "$$stage" "$$partial"
	  exit "$$status"
	}
	trap cleanup EXIT
	quiesce_project "$$project"
	mkdir -p "$$stage/volumes" "$$stage/config"
	echo 'Backing up PostgreSQL...'
	dump_database "$$stage/artifact_registry.dump"
	for vol in $(DATA_VOLUMES); do
	  if ! actual="$$(volume_name "$$project" "$$vol")"; then
	    if [[ " $(REQUIRED_VOLUMES) " == *" $$vol "* ]]; then
	      fail "Required Docker volume missing: $$vol"
	    fi
	    echo "Skipping non-existent optional volume: $$vol"
	    continue
	  fi
	  echo "Archiving $$vol ($$actual)..."
	  docker run --rm --network none --user 0:0 --entrypoint /bin/sh \
	    --mount "type=volume,source=$$actual,target=/volume,readonly" \
	    "$$image" -ec 'exec tar -cf - -C /volume .' > "$$stage/volumes/$$vol.tar"
	done
	[[ -s "$$stage/volumes/artifact_storage.tar" ]] || fail 'Critical artifact_storage archive is missing'
	# Copy deployment config for reference, never automatically overwrite it on restore.
	for file in '$(MAKEFILE_DIR)/.env' '$(MAKEFILE_DIR)/docker-compose.yml' \
	            '$(MAKEFILE_DIR)/../.env' '$(MAKEFILE_DIR)/../docker-compose.yml'; do
	  if [[ -f "$$file" ]]; then
	    case "$$file" in
	      '$(MAKEFILE_DIR)'/*) prefix=local ;;
	      *) prefix=parent ;;
	    esac
	    cp -- "$$file" "$$stage/config/$$prefix-$$(basename "$$file")"
	  fi
	done
	cp -- '$(abspath $(firstword $(MAKEFILE_LIST)))' "$$stage/config/backup-Makefile"
	printf 'format=artifact-keeper-backup-v1\ncreated=%s\nsource_project=%s\n' \
	  '$(TIMESTAMP)' "$$project" > "$$stage/MANIFEST"
	(cd "$$stage" && sha256sum MANIFEST artifact_registry.dump volumes/*.tar config/* > SHA256SUMS)
	echo 'Creating compressed full backup archive...'
	tar -czf "$$partial" -C "$$stage" .
	tar -tzf "$$partial" > /dev/null
	mv -- "$$partial" "$$archive"
	prune_backups
	echo "Full backup: $$archive"

restore-all:
	@$(HELPERS)
	check_db
	confirm_restore
	project="$$(project_name)"
	image="$$(postgres_image)"
	mkdir -p -- '$(BACKUP_DIR_ABS)'
	stage="$$(mktemp -d '$(BACKUP_DIR_ABS)/.restore-stage.XXXXXXXX')"
	stopped=()
	completed=0
	cleanup() {
	  status=$$?
	  trap - EXIT
	  rm -rf -- "$$stage"
	  if (( status == 0 && completed == 1 )); then
	    start_stopped || status=1
	  else
	    echo 'Restore failed: application containers are intentionally left stopped if they were stopped.' >&2
	  fi
	  exit "$$status"
	}
	trap cleanup EXIT
	echo 'Validating and extracting full backup before touching live data...'
	tar -tzf '$(RESTORE_FILE)' > /dev/null
	tar -xzf '$(RESTORE_FILE)' -C "$$stage" --no-same-owner
	check_file "$$stage/MANIFEST"
	check_file "$$stage/SHA256SUMS"
	grep -qx 'format=artifact-keeper-backup-v1' "$$stage/MANIFEST" || fail 'Unexpected backup format'
	(cd "$$stage" && sha256sum --check --status SHA256SUMS) || fail 'Backup checksum verification failed'
	validate_dump "$$stage/artifact_registry.dump"
	check_file "$$stage/volumes/artifact_storage.tar"
	# Pre-validate all permitted archives AND destination volumes before stopping.
	for vol in $(DATA_VOLUMES); do
	  file="$$stage/volumes/$$vol.tar"
	  [[ -f "$$file" ]] || continue
	  tar -tf "$$file" > /dev/null || fail "Invalid volume archive: $$vol"
	  actual="$$(volume_name "$$project" "$$vol")" || fail "Missing destination volume: $$vol (create it with Docker Compose first)"
	  echo "Validated $$vol -> $$actual"
	done
	quiesce_project "$$project"
	# Each volume is cleared before extraction to avoid leftover stale files.
	for vol in $(DATA_VOLUMES); do
	  file="$$stage/volumes/$$vol.tar"
	  [[ -f "$$file" ]] || continue
	  actual="$$(volume_name "$$project" "$$vol")"
	  echo "Restoring $$vol ($$actual)..."
	  docker run --rm -i --network none --user 0:0 --entrypoint /bin/sh \
	    --mount "type=volume,source=$$actual,target=/volume" \
	    "$$image" -ec '
	      find /volume -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
	      exec tar -xf - -C /volume
	    ' < "$$file"
	done
	echo 'Restoring PostgreSQL...'
	restore_database "$$stage/artifact_registry.dump"
	completed=1
	echo 'Full restore completed. Config snapshots were NOT automatically applied.'

verify:
	@$(HELPERS)
	check_db
	[[ -n '$(RESTORE_FILE)' ]] || fail 'Usage: make verify RESTORE_FILE=<file.dump or file.tar.gz>'
	check_file '$(RESTORE_FILE)'
	case '$(RESTORE_FILE)' in
	  *.dump) validate_dump '$(RESTORE_FILE)' ;;
	  *.tar.gz)
	    tar -tzf '$(RESTORE_FILE)' >/dev/null
	    stage="$$(mktemp -d)"
	    trap 'rm -rf -- "$$stage"' EXIT
	    tar -xzf '$(RESTORE_FILE)' -C "$$stage" --no-same-owner
	    (cd "$$stage" && sha256sum --check SHA256SUMS)
	    validate_dump "$$stage/artifact_registry.dump"
	    for file in "$$stage"/volumes/*.tar; do [[ -e "$$file" ]] && tar -tf "$$file" >/dev/null; done
	    ;;
	  *) fail 'Unknown file extension; expected .dump or .tar.gz' ;;
	esac
	echo 'Backup validation passed (not a test restore).'

clean:
	@$(HELPERS)
	mkdir -p -- '$(BACKUP_DIR_ABS)'
	shopt -s nullglob
	files=('$(BACKUP_DIR_ABS)'/artifact_registry_*.dump '$(BACKUP_DIR_ABS)'/artifact_keeper_*.tar.gz)
	if (( $${#files[@]} )); then rm -f -- "$${files[@]}"; fi
	echo 'Removed generated backup archives (staging directories, if any, were not deleted).'

help:
	@printf '%s\n' \
	  'Artifact Keeper backup and recovery' \
	  '' \
	  '  make backup                         - Online PostgreSQL backup (.dump)' \
	  '  make restore RESTORE_FILE=... CONFIRM=YES' \
	  '                                      - Restore PostgreSQL (destructive)' \
	  '  make backup-all                     - DB + persistent Docker volumes' \
	  '  make restore-all RESTORE_FILE=... CONFIRM=YES' \
	  '                                      - Restore DB + volumes (destructive)' \
	  '  make verify RESTORE_FILE=...       - Check archive/dump integrity' \
	  '  make clean                         - Delete generated backup archives' \
	  '  make help                          - Show this help' \
	  '' \
	  'Configuration: POSTGRES_CONTAINER, BACKUP_DIR, BACKUPS_TO_KEEP (default 10),' \
	  '               DATA_VOLUMES, REQUIRED_VOLUMES, BACKUP_FILE, FULL_BACKUP_FILE.' \
	  'Full backups temporarily stop other running containers in the same Compose project.' \
	  'Restores require existing destination volumes and CONFIRM=YES.'

