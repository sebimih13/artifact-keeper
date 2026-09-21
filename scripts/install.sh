#!/usr/bin/env bash
# =============================================================================
# Artifact Keeper - Quickstart Installer
# =============================================================================
#
# One-command setup for evaluating Artifact Keeper locally.
# Detects Docker or Podman, generates secure credentials, downloads the
# compose file, and starts the full stack.
#
# Usage:
#   curl -fsSL https://get.artifactkeeper.com | bash
#
# Or run directly:
#   bash scripts/install.sh
#
# Options (environment variables):
#   INSTALL_DIR       - Where to create the project directory (default: ./artifact-keeper)
#   AK_VERSION        - Version to install (default: latest)
#   AK_HTTP_PORT      - HTTP port (default: 80)
#   AK_HTTPS_PORT     - HTTPS port (default: 443)
#   AK_ADMIN_PASSWORD - Admin password (default: auto-generated)
#   AK_SKIP_START     - Set to 1 to generate files without starting (default: 0)
#   AK_MINIMAL        - Set to 1 to skip scanners (Trivy, OpenSCAP, Dependency-Track)
#
# Requirements:
#   - Docker 20.10+ with Compose V2, or Podman 4+ with podman-compose
#   - 2 GB RAM minimum (4 GB recommended with scanners enabled)
#   - Ports 80 and 443 available (configurable)
#
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

INSTALL_DIR="${INSTALL_DIR:-./artifact-keeper}"
AK_VERSION="${AK_VERSION:-latest}"
AK_HTTP_PORT="${AK_HTTP_PORT:-80}"
AK_HTTPS_PORT="${AK_HTTPS_PORT:-443}"
AK_ADMIN_PASSWORD="${AK_ADMIN_PASSWORD:-}"
AK_SKIP_START="${AK_SKIP_START:-0}"
AK_MINIMAL="${AK_MINIMAL:-0}"

REPO_BASE="https://raw.githubusercontent.com/artifact-keeper/artifact-keeper/main"
# Prefer the matching configuration when invoked from a source checkout.
SOURCE_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ]; then
    SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

info()  { printf "\033[1;34m==>\033[0m %s\n" "$*"; }
ok()    { printf "\033[1;32m==>\033[0m %s\n" "$*"; }
warn()  { printf "\033[1;33m==>\033[0m %s\n" "$*" >&2; }
die()   { printf "\033[1;31mError:\033[0m %s\n" "$*" >&2; exit 1; }

generate_password() {
    # Read finite input so pipefail cannot turn head's SIGPIPE into a failure.
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 12
    elif [ -r /dev/urandom ]; then
        od -An -N12 -tx1 /dev/urandom | tr -d ' \n'
    else
        die "Secure random generation requires openssl or /dev/urandom."
    fi
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

info "Artifact Keeper Quickstart Installer"
echo ""

# Detect container runtime
COMPOSE_CMD=""
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
    info "Found Docker with Compose V2"
elif command -v docker >/dev/null 2>&1 && command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
    info "Found Docker with docker-compose"
elif command -v podman >/dev/null 2>&1 && command -v podman-compose >/dev/null 2>&1; then
    COMPOSE_CMD="podman-compose"
    info "Found Podman with podman-compose"
elif command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
    COMPOSE_CMD="podman compose"
    info "Found Podman with Compose"
else
    echo ""
    die "No container runtime found.

Artifact Keeper requires Docker or Podman to run.

Install Docker:
  Linux:   https://docs.docker.com/engine/install/
  macOS:   https://docs.docker.com/desktop/install/mac-install/
  Windows: https://docs.docker.com/desktop/install/windows-install/

Or install Podman:
  https://podman.io/docs/installation"
fi

# Check if Docker daemon is running
if [ "$AK_SKIP_START" != "1" ] && [[ "$COMPOSE_CMD" == *docker* ]]; then
    if ! docker info >/dev/null 2>&1; then
        die "Cannot access the Docker daemon. Check that it is running and that your user can access its socket.
Start it with: sudo systemctl start docker (Linux) or open Docker Desktop (macOS/Windows)."
    fi
fi

# Check port availability
for port in "$AK_HTTP_PORT" "$AK_HTTPS_PORT"; do
    if command -v lsof >/dev/null 2>&1; then
        if lsof -i :"$port" -sTCP:LISTEN >/dev/null 2>&1; then
            warn "Port $port is already in use. Set AK_HTTP_PORT/AK_HTTPS_PORT to use different ports."
            warn "Example: AK_HTTP_PORT=8080 AK_HTTPS_PORT=8443 bash install.sh"
        fi
    fi
done

# ---------------------------------------------------------------------------
# Generate credentials
# ---------------------------------------------------------------------------

info "Generating secure credentials..."

random_base64() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 "$1"
    else
        dd if=/dev/urandom bs="$1" count=1 2>/dev/null | base64 | tr -d '\n'
    fi
}

JWT_SECRET="$(random_base64 48)"
# Webhook signing secrets require exactly 32 random bytes, base64 encoded.
WEBHOOK_SECRET_KEY="$(random_base64 32)"

if [ -z "$AK_ADMIN_PASSWORD" ]; then
    AK_ADMIN_PASSWORD="$(generate_password)"
    ADMIN_PW_GENERATED=1
else
    ADMIN_PW_GENERATED=0
fi

# ---------------------------------------------------------------------------
# Create project directory
# ---------------------------------------------------------------------------

if [ -d "$INSTALL_DIR" ]; then
    if [ -f "$INSTALL_DIR/docker-compose.yml" ]; then
        warn "Directory $INSTALL_DIR already exists with a compose file."
        warn "To reinstall, remove it first: rm -rf $INSTALL_DIR"
        die "Installation directory already exists."
    fi
fi

mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"
INSTALL_DIR="$(pwd)"

info "Installing to $INSTALL_DIR"

# ---------------------------------------------------------------------------
# Download compose file and supporting files from the repo
# ---------------------------------------------------------------------------

info "Preparing configuration files..."

mkdir -p docker
for file in docker-compose.yml docker/Caddyfile docker/init-db.sql docker/init-pg-ssl.sh docker/init-dtrack.sh; do
    if [ -n "$SOURCE_DIR" ] && [ -f "$SOURCE_DIR/docker-compose.yml" ]; then
        cp "$SOURCE_DIR/$file" "$file"
    else
        curl -fsSL "$REPO_BASE/$file" -o "$file"
    fi
done
chmod +x docker/init-pg-ssl.sh docker/init-dtrack.sh

# ---------------------------------------------------------------------------
# Write .env file
# ---------------------------------------------------------------------------

info "Writing configuration..."

# Compose single quotes preserve dollar signs and hashes in supplied passwords.
case "$AK_ADMIN_PASSWORD" in
    *$'\n'*|*$'\r'*) die "AK_ADMIN_PASSWORD must be a single line." ;;
esac
ENV_ADMIN_PASSWORD="${AK_ADMIN_PASSWORD//\'/\\\'}"
umask 077
cat > .env <<ENVEOF
# Artifact Keeper configuration
# Generated by the quickstart installer on $(date -u +"%Y-%m-%d %H:%M:%S UTC")

# Version (omit the "v" prefix)
ARTIFACT_KEEPER_VERSION=${AK_VERSION}

# Admin password for the web UI
ADMIN_PASSWORD='${ENV_ADMIN_PASSWORD}'

# Security keys (auto-generated, keep these secret)
JWT_SECRET=${JWT_SECRET}
AK_WEBHOOK_SECRET_KEY=${WEBHOOK_SECRET_KEY}

# Search backend (OpenSearch runs in single-node mode with the security
# plugin disabled for local self-host deployments. The service is bound to
# the internal docker network only and is not exposed to the host. For
# multi-node or public-facing setups, enable the security plugin and set
# OPENSEARCH_USERNAME / OPENSEARCH_PASSWORD here.)
# OPENSEARCH_USERNAME=
# OPENSEARCH_PASSWORD=

# Ports
HTTP_PORT=${AK_HTTP_PORT}
HTTPS_PORT=${AK_HTTPS_PORT}

# Site address (set to your domain for automatic TLS via Let's Encrypt)
SITE_ADDRESS=localhost

# Environment
ENVIRONMENT=production
RUST_LOG=info

# Dependency-Track is opt-in: set true and start with --profile dtrack.
DEPENDENCY_TRACK_ENABLED=false
ENVEOF

# If minimal mode, disable scanners
if [ "$AK_MINIMAL" = "1" ]; then
    # Persist minimal mode so subsequent `compose up` also skips scanners.
    cat > docker-compose.override.yml <<'YAMLEOF'
services:
  trivy:
    profiles: [scanners]
  openscap:
    profiles: [scanners]
  backend:
    environment:
      TRIVY_URL: null
      OPENSCAP_URL: null
YAMLEOF
    info "Minimal mode: scanners disabled"
fi

# ---------------------------------------------------------------------------
# Pull images and start
# ---------------------------------------------------------------------------

if [ "$AK_SKIP_START" = "1" ]; then
    ok "Configuration written to $INSTALL_DIR"
    echo ""
    echo "  To start: cd $INSTALL_DIR && $COMPOSE_CMD up -d"
    echo ""
    exit 0
fi

info "Pulling container images (this may take a few minutes)..."

$COMPOSE_CMD config --quiet
$COMPOSE_CMD pull

info "Starting Artifact Keeper..."
$COMPOSE_CMD up -d

# ---------------------------------------------------------------------------
# Wait for health
# ---------------------------------------------------------------------------

info "Waiting for services to be ready..."

BACKEND_READY=0
for i in $(seq 1 60); do
    if curl -sf "http://localhost:${AK_HTTP_PORT}/health" >/dev/null 2>&1 || \
       curl -sf "http://localhost:${AK_HTTP_PORT}/livez" >/dev/null 2>&1; then
        BACKEND_READY=1
        break
    fi
    printf "."
    sleep 2
done
echo ""

if [ "$BACKEND_READY" = "0" ]; then
    warn "Services are still starting up. Check status with:"
    echo "  cd $INSTALL_DIR && $COMPOSE_CMD ps"
    echo "  cd $INSTALL_DIR && $COMPOSE_CMD logs backend"
    echo ""
    die "Artifact Keeper did not become ready. Inspect the logs above before retrying."
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
ok "Artifact Keeper is running!"
echo ""
echo "  Web UI:    http://localhost:${AK_HTTP_PORT}"
echo "  API:       http://localhost:${AK_HTTP_PORT}/api/v1"
echo "  Swagger:   http://localhost:${AK_HTTP_PORT}/swagger-ui (set ENABLE_SWAGGER=true to serve it)"
echo ""
echo "  Username:  admin"
if [ "$ADMIN_PW_GENERATED" = "1" ]; then
echo "  Password:  ${AK_ADMIN_PASSWORD}"
echo ""
echo "  (password saved in $INSTALL_DIR/.env)"
fi
echo ""
echo "  Quick start:"
echo "    # Create a repository"
echo "    curl -u admin:${AK_ADMIN_PASSWORD} http://localhost:${AK_HTTP_PORT}/api/v1/repositories \\"
echo "      -H 'Content-Type: application/json' \\"
echo "      -d '{\"key\":\"my-npm\",\"name\":\"My NPM\",\"format\":\"npm\",\"repo_type\":\"local\"}'"
echo ""
echo "  Manage:"
echo "    cd $INSTALL_DIR"
echo "    $COMPOSE_CMD ps          # status"
echo "    $COMPOSE_CMD logs -f     # logs"
echo "    $COMPOSE_CMD down        # stop"
echo "    $COMPOSE_CMD up -d       # start"
echo ""
echo "  Docs: https://artifactkeeper.com/docs"
echo ""

# Try to open browser (best effort, non-blocking)
if [ "$BACKEND_READY" = "1" ]; then
    if command -v open >/dev/null 2>&1; then
        open "http://localhost:${AK_HTTP_PORT}" 2>/dev/null || true
    elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "http://localhost:${AK_HTTP_PORT}" 2>/dev/null || true
    fi
fi
