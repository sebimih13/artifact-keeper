# Docker image seeder

`seed-docker.py` builds or pulls images, retags them under an Artifact Keeper
Docker repository, and pushes them. It uses Python 3.9+ with no Python packages,
Docker Engine/CLI 28+ with Buildx, and Compose v2+ for Compose mode. Docker must
be running and your user must have permission to access it.

## Repository and environment

Create a **local Docker-format repository**, for example with key `docker`,
and grant your token permission to push and pull it.

```sh
export ARTIFACT_KEEPER_TOKEN='your-token'
export AK_API='https://localhost/docker'
```

Here `AK_API` means `https://REGISTRY_HOST/REPOSITORY_KEY`. It is a naming
convention for this script; Docker actually uses `/v2/` on the registry host.
`https://localhost/npm/npm` is an npm endpoint and cannot receive Docker images.
For a non-default port, use `https://localhost:30443/docker`.

| Variable | Purpose |
| --- | --- |
| `ARTIFACT_KEEPER_TOKEN` | Required for seeding; never put it into Docker build arguments. |
| `AK_API` | Seeder destination; `--artifact-keeper` overrides it. |
| `DOCKER` | Optional Docker executable path; defaults to `docker`. |
| `DOCKER_HOST`, `DOCKER_CONTEXT`, `DOCKER_TLS_VERIFY`, `DOCKER_CERT_PATH` | Optional standard Docker settings for accessing a different daemon. |
| `DOCKER_CONFIG` | Optional existing Docker client configuration, including upstream registry credentials and contexts. |

The script uses your existing source-registry credentials. Authenticate to a
private upstream with `docker login ghcr.io`, for example. Target credentials
are stored in a temporary mode-0600 Docker configuration, removed after the run;
your existing Docker login configuration is not modified.

## TLS trust

The Docker daemon must trust Artifact Keeper's HTTPS certificate. Setting
`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE` or a Python CA option does **not** configure
Docker's registry trust. For native Linux Docker Engine, install your local CA
on the **daemon host**, using the exact host/port from image references:

```sh
export AK_REGISTRY=localhost
sudo install -D -m 0644 /path/to/caddy-root.crt \
  "/etc/docker/certs.d/${AK_REGISTRY}/ca.crt"
```

For this deployment, the public Caddy CA can be copied with:

```sh
docker cp artifact-keeper-caddy:/data/caddy/pki/authorities/local/root.crt /tmp/caddy-root.crt
```

Use `/tmp/caddy-root.crt` in the install command. Docker Desktop and remote
BuildKit builders have their own trust setup; configure those separately.
Do not disable certificate verification. Restart Docker if required by your
installation, coordinating that restart with running workloads.

## Seed one image

```sh
python3 seeders/docker/seed-docker.py --image nginx
python3 seeders/docker/seed-docker.py --image nginx:1.27
python3 seeders/docker/seed-docker.py --image 'nginx@sha256:<actual-64-hex-digest>'
python3 seeders/docker/seed-docker.py --image ghcr.io/owner/image:tag
python3 seeders/docker/seed-docker.py --image bitnami/postgresql:16
```

These are reference forms, not guarantees that a particular upstream tag exists
or is publicly accessible. A missing tag or denied upstream pull is a failure.

The default naming retains the source registry and namespace:

| Source | Target with `AK_API=https://localhost/docker` |
| --- | --- |
| `nginx` | `localhost/docker/docker.io/library/nginx:latest` |
| `nginx:1.27` | `localhost/docker/docker.io/library/nginx:1.27` |
| `nginx@sha256:HASH` | `localhost/docker/docker.io/library/nginx:sha256-HASH` |
| `ghcr.io/owner/image:tag` | `localhost/docker/ghcr.io/owner/image:tag` |
| `bitnami/postgresql:16` | `localhost/docker/docker.io/bitnami/postgresql:16` |
| `registry.example:5000/team/app:1` | `localhost/docker/registry.example-port-5000/team/app:1` |

Registry port separators become `-port-` in target paths. Use `--destination`
if that encoding conflicts with another source hostname. IPv6 source registry
literals are not supported by this script's reference parser.

To choose a different destination name:

```sh
python3 seeders/docker/seed-docker.py --image nginx:1.27 \
  --destination mirrors/nginx:1.27 --platform linux/amd64
```

## Build an internal image

```sh
python3 seeders/docker/seed-docker.py \
  --dockerfile /path/to/project/docker/Dockerfile \
  --context /path/to/project \
  --destination internal/my-app:1.0.0 \
  --platform linux/amd64
```

`--context` defaults to the Dockerfile's parent directory. Optional `--target`
selects a build stage and repeated `--build-arg KEY=VALUE` supplies build
arguments. Use `.dockerignore` to control the build context. The Dockerfile's
instructions run normally; base images are pulled. The resulting application
image is published, not each base image as a separate repository entry.

## Seed a Compose file

```sh
python3 seeders/docker/seed-docker.py \
  --docker-compose-file /path/to/compose.yml

python3 seeders/docker/seed-docker.py \
  --docker-compose-file /path/to/compose.yml \
  --docker-compose-file /path/to/compose.override.yml \
  --env-file /path/to/.env
```

The script delegates parsing and interpolation to `docker compose config` and
includes **all profiles**. Repeated files merge in the given order. Service
images are deduplicated; services with `build:` are built, including services
that also specify `image:`. Build-only services are published under
`internal/<compose-project>/<service>:latest`. It does not run `compose up`.
Build dependencies and base images may be downloaded by the builder, but only
final service images are published. Arbitrary images referenced in commands,
Dockerfile stages or external tooling are not discovered as service images.

Service `platform:` is respected. An explicit conflicting `--platform`, a
multi-platform `build.platforms`, or different builds/platforms mapping to one
target tag fails before uploading. Assign distinct tags for different variants.
Named services sharing an image but having different build definitions are
not silently collapsed. The script does not rewrite the original Compose file;
update its `image:` values to the printed destination names before deploying.

## Pull from Artifact Keeper

For the native Docker CLI, the seeder's environment variables do not perform
login automatically. Set a registry host and repository key, then log in:

```sh
export AK_REGISTRY=localhost           # host[:port], no https:// or path
export AK_REPOSITORY=docker
export ARTIFACT_KEEPER_TOKEN='your-token'

printf '%s' "$ARTIFACT_KEEPER_TOKEN" |
  docker login "$AK_REGISTRY" --username __token__ --password-stdin

docker pull "$AK_REGISTRY/$AK_REPOSITORY/docker.io/library/nginx:1.27"
docker pull "$AK_REGISTRY/$AK_REPOSITORY/internal/my-app:1.0.0"
```

`AK_REGISTRY` and `AK_REPOSITORY` are conveniences for these shell commands,
not Docker configuration variables. After login, Docker uses its saved
credentials; the token need not remain exported for every pull. Anonymous
pulls depend on repository permissions. `localhost` refers to the Docker daemon
host: use a reachable registry hostname when the daemon is remote.

A successful push prints a destination digest when Docker reports it. Use that
value for immutable pulls:

```sh
docker pull "$AK_REGISTRY/$AK_REPOSITORY/internal/my-app@sha256:<destination-digest>"
```

## Platform, digest and summary behavior

This script selects **one platform**, defaulting to the Docker daemon's
OS/architecture. It does not replicate a complete multi-platform index,
attestations, signatures, referrers or every upstream tag. A source digest can
identify an index, while the pushed destination identifies a selected-platform
manifest. Therefore the source and destination digests may differ. A
`sha256-HASH` destination tag records the requested source digest; it is not a
claim that the destination manifest has that digest. Use a registry-to-registry
copy tool with all-platform support for full multi-platform replication.

Mutable tags are pulled again and pushed on every run. Docker reuses existing
layers. The script does not skip a remote tag merely because it exists.
`Uploaded` counts successful image pushes, including successful repeat pushes;
`Skipped` counts duplicate Compose image entries; `Failed` counts image jobs
that could not complete. A nonzero failure count returns exit status 1.
Temporary authentication files are removed, but images, destination tags and
build cache remain in Docker. No global prune or image removal is performed.

References: [Docker pull](https://docs.docker.com/reference/cli/docker/image/pull/),
[Docker push](https://docs.docker.com/reference/cli/docker/image/push/),
[Compose config](https://docs.docker.com/reference/cli/docker/compose/config/),
[registry certificate trust](https://docs.docker.com/engine/security/certificates/).


# TODO: Use a registry mirror
Edit `/etc/docker/daemon.json`:
```json
{
  "registry-mirrors": [
    "https://artifacts.mycompany.com:8080"
  ]
}
```

