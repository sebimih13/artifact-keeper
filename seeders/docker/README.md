# Docker image seeder: reuse local images, pull missing images

`seed-docker.py` reuses images already present in the selected Docker daemon.
If a named image or selected platform is missing, it runs `docker pull`, then
publishes the image to Artifact Keeper. Existing local tags are not refreshed.
It never runs `docker build` or `docker compose build`. Pull failures are
reported as failed image jobs; daemon, permission and inspection errors are
not treated as missing images.
It does not start containers, prune images or change your saved Docker logins.

Requires Python 3.9+, Docker Engine/CLI 28+, and Compose v2+ for Compose input.
Use the same daemon/context that contains the images you built or loaded.
A Dockerfile is not a stored image: `--dockerfile`, `--context`, `--build-arg`
and `--target` have been removed. Select the existing image with `--image`.
Buildx outputs that exist only in a builder cache must first be loaded into
the daemon (for example, using `--load` during your own build).

## Publish an existing image

```sh
export ARTIFACT_KEEPER_TOKEN='your-token'
export AK_API='https://localhost/docker'

python3 seeders/docker/seed-docker.py --image my-app:1.0.0
python3 seeders/docker/seed-docker.py --image nginx:1.27
python3 seeders/docker/seed-docker.py --image my-org/my-app:1.0.0
```

`AK_API` is `https://REGISTRY_HOST/REPOSITORY_KEY`, not an npm URL. Docker uses
`/v2/` on the registry host. A local Docker-format repository named `docker`
must exist, and the token must have push permission.

| Variable | Purpose |
| --- | --- |
| `ARTIFACT_KEEPER_TOKEN` | Required by the seeder to push. |
| `AK_API` | Seeder destination; overridden by `--artifact-keeper`. |
| `DOCKER` | Optional Docker executable; defaults to `docker`. |
| `DOCKER_HOST`, `DOCKER_CONTEXT` | Select the daemon/context containing the local images. |
| `DOCKER_TLS_VERIFY`, `DOCKER_CERT_PATH` | Optional TLS settings for contacting a remote daemon, not registry trust. |
| `DOCKER_CONFIG` | Existing client config/context location; target credentials remain temporary. |

Names, tags and digest references are supported. An omitted tag means `latest`.
Missing images are pulled using your existing Docker credentials and daemon
mirror configuration. For private upstreams, log in to that registry first.
Temporary target credentials use a mode-0600 configuration that is removed
after each push. Images and destination tags remain in the daemon.

## Publish images used by Compose

```sh
python3 seeders/docker/seed-docker.py \
  --docker-compose-file /path/to/compose.yml \
  --env-file /path/to/.env
```

Repeat `--docker-compose-file` to merge overrides in order. All profiles are
included, and Docker Compose resolves interpolation and includes. Duplicate
image/platform entries are published once. A `build:` section is never run.

Services with `image:` reuse the local image or pull it if missing, even when
`build:` is also present. Build-only services use the local
`<project>-<service>:latest` image and fail if it is missing: there is no explicit
registry image to pull. Build/load those separately or add a pullable `image:`. Supply `--project-name NAME` if the previous build used `docker compose
-p NAME`. Explicit `image:` names are preferable for deployment: a build-only
service has no explicit registry image name for `compose pull` to use.

`--platform linux/amd64` selects a local platform; service `platform:` is
respected. A conflicting explicit platform or different platforms mapped to
one destination tag fails. The script does not modify your Compose file.

## Keep the same image names with Docker Hub mirror mode

Configure the **backend container**, not merely the Compose interpolation file:

```ini
AK_DEFAULT_DOCKER_MIRROR_REPO=docker
```

A Compose `.env` entry only takes effect if the service passes it into its
container, for example:

```yaml
environment:
  AK_DEFAULT_DOCKER_MIRROR_REPO: ${AK_DEFAULT_DOCKER_MIRROR_REPO:-docker}
```

Recreate/restart the backend after changing its environment: this setting is
cached for the backend process lifetime. Configure the **Docker daemon host**:

```json
{
  "registry-mirrors": ["https://localhost"]
}
```

Reload/restart Docker as appropriate after changing daemon configuration,
coordinating restarts with running workloads. Confirm the active mirror with:

```sh
docker info --format '{{json .RegistryConfig.Mirrors}}'
```

The seeder now uses mirror-compatible names:

| Existing local image | Published image | Unchanged mirror pull |
| --- | --- | --- |
| `nginx:1.27` | `localhost/docker/library/nginx:1.27` | `docker pull nginx:1.27` |
| `my-app:1.0.0` | `localhost/docker/library/my-app:1.0.0` | `docker pull my-app:1.0.0` |
| `my-org/my-app:1.0.0` | `localhost/docker/my-org/my-app:1.0.0` | `docker pull my-org/my-app:1.0.0` |

Docker's Hub request `/v2/library/nginx/...` is routed to the `docker`
repository, with image path `library/nginx`. The previous seeder's
`docker.io/library/nginx` path does **not** match. Rerun the seeder against your
existing local images to populate the corrected paths; old artifacts are not
deleted. An optional `--destination name:tag` changes the target name and hence
which unchanged image name can resolve through the mirror.

Then your deployment may keep `image: my-app:1.0.0` and use:

```sh
docker compose -f /path/to/compose.yml pull
docker compose -f /path/to/compose.yml up -d --no-build --pull never
```

The first command pulls; the second uses the pulled local images without
building or pulling again. `docker pull` retrieves a registry image and never
builds a Dockerfile. If an image already exists locally, Docker can run it
without any pull at all (`docker run --pull=never ...`).

### Limits and access

- Docker daemon `registry-mirrors` applies to **Docker Hub only**. It does not
  redirect `ghcr.io/...`, ECR or other explicit registry hosts. Those Compose
  entries must use explicit Artifact Keeper names to pull from it, or need a
  different runtime/configuration that supports those registry rewrites.
- For example, `ghcr.io/owner/app:tag` is published to
  `localhost/docker/ghcr.io/owner/app:tag`; an unchanged `ghcr.io/...` pull still
  contacts GHCR. Your Artifact Keeper deployment Compose file contains GHCR
  images, so this daemon setting alone cannot redirect every service image.
- Mirror mode preserves repository read permissions. The daemon must be able
  to obtain a pull token for the mirror; do not assume credentials from an
  explicit `docker login localhost` are used identically in every Docker
  version's Hub-mirror flow. Test the unchanged name. Authentication failures
  can cause fallback to Docker Hub. The tested local `docker` repository is
  currently public; we did not change its visibility.
- A local repository serves seeded images; it is not automatically an upstream
  pull-through cache. Docker can fall back to Docker Hub when the mirror misses.
- Artifact Keeper tries a literal repository key before mirror fallback. An
  image namespace that equals an existing repository key can route differently.
- `localhost` refers to the daemon host. Remote daemons need a reachable registry
  hostname, their own mirror setting and their own CA trust.

## Explicit pulls and certificate trust

For explicit pulls (also useful for non-Hub sources):

```sh
export AK_REGISTRY=localhost
export AK_REPOSITORY=docker
export ARTIFACT_KEEPER_TOKEN='your-token'
printf '%s' "$ARTIFACT_KEEPER_TOKEN" |
  docker login "$AK_REGISTRY" --username __token__ --password-stdin

docker pull "$AK_REGISTRY/$AK_REPOSITORY/library/my-app:1.0.0"
docker pull "$AK_REGISTRY/$AK_REPOSITORY/ghcr.io/owner/app:tag"
```

`AK_REGISTRY` and `AK_REPOSITORY` are shell conveniences, not Docker settings.
Python CA variables such as `SSL_CERT_FILE` do not configure Docker trust.
On native Linux, install the public CA on the daemon host under the exact
registry host/port name:

```sh
docker cp artifact-keeper-caddy:/data/caddy/pki/authorities/local/root.crt /tmp/caddy-root.crt
sudo install -D -m 0644 /tmp/caddy-root.crt \
  "/etc/docker/certs.d/${AK_REGISTRY}/ca.crt"
```

Docker Desktop has separate trust setup. Do not disable certificate verification.

## Platforms, digests and summary

One platform is published, defaulting to the daemon's OS/architecture. This
is not full multi-platform replication: indexes, signatures, attestations and
referrers are not preserved. A source index digest can differ from the pushed
platform manifest digest. Digest inputs use a `sha256-HASH` destination tag;
use the printed destination digest for immutable pulls. Unchanged **source
index digest** pulls are not guaranteed to work through this single-platform
mirror workflow.

`Uploaded` counts successful pushes, including repeat pushes. Docker reuses
existing layers. `Skipped` counts duplicate Compose entries; `Failed` counts
image jobs that failed, including failed pulls and missing build-only images. Failures return a
nonzero exit status. The summary uses the same fields as the PyPI seeder.

References: [Docker Hub mirrors](https://docs.docker.com/docker-hub/image-library/mirror/),
[Docker pull](https://docs.docker.com/reference/cli/docker/image/pull/),
[registry certificate trust](https://docs.docker.com/engine/security/certificates/).
