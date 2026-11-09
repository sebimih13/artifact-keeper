# Debian / APT seeder and Ubuntu clients

Upload existing `.deb` packages into **`apt-local-devtools`**, an Artifact Keeper **local** repository with format **`debian`**, then install them through APT on Ubuntu amd64 and arm64. The name `apt-local-devops` in the original request is treated as a typo; every example uses `apt-local-devtools`.

## Design and existing seeder conventions

The existing Cargo, Docker and PyPI seeders supplied the conventions for this script: a token in `ARTIFACT_KEEPER_TOKEN`, a configurable `AK_API`, verified TLS, explicit inputs, successful duplicate skips, temporary/build output kept separate from source, and upload/skip/failure totals. URL conventions differ between existing seeders; **this script takes a server base URL**, such as `https://localhost`, without `/api/v1`, `/debian` or a repository key.

The other seeders illustrate related flows: Maven resolves POM dependencies or uploads an existing JAR, Go mirrors modules or publishes a project, npm mirrors registry dependencies from manifests/locks, and P2 generates and uploads update-site metadata. Debian uses a native package-upload route and lets Artifact Keeper produce APT metadata; the separate demo builder provides reproducible input packages.

Unlike dependency-mirroring seeders, this script publishes `.deb` files you already have. It does not resolve dependencies, mirror Ubuntu, compile projects, or install/execute uploaded packages. It uses the backend's Debian upload endpoint so that the server generates APT indexes and signed Release metadata from package control fields.

| Setting | Value |
| --- | --- |
| Repository key and display name | `apt-local-devtools` |
| Format / type | `debian` / `local` |
| Read visibility for this demo | Public; writes require authentication |
| Distribution / component | `stable` / `main` |
| Architectures demonstrated | `amd64`, `arm64` |
| Repository root | `https://YOUR-SERVER/debian/apt-local-devtools` |
| Public signing key | Repository root + `/gpg-key.asc` |
| Signed metadata | Repository root + `/dists/stable/InRelease` |
| Index example | Repository root + `/dists/stable/main/binary-amd64/Packages` |
| Upload example | `PUT /debian/apt-local-devtools/pool/main/a/ak-devtools-hello/ak-devtools-hello_1.0.0-1_amd64.deb` |

The hosted backend currently derives distributions from the requested metadata URL and indexes packages by component/architecture; uploads do not select an Ubuntu codename. `stable` is the label used consistently here. The `debian` API configuration for distribution/architecture filters applies to remote repositories, not this hosted repository. For production packages with release-specific dependencies, use separate repositories per supported Ubuntu/Debian release and test on each target.

## Requirements and credentials

- Python **3.10+**, standard library only; no pip packages or root access needed for seeding/building the samples.
- An existing reachable Artifact Keeper server. Creation/signing needs an administrator token. Subsequent uploads need repository read access, signing-config visibility and `write:artifacts` permission.
- Gzip, xz, bzip2 and uncompressed control tar archives work through Python. Zstandard-compressed control archives require `dpkg-deb` on PATH (`sudo apt install dpkg`). The server must also support the archive compression used by your package; the demo uses gzip for compatibility.
- Docker Engine and Compose v2 for the clients. ARM64 needs a native ARM host or already configured QEMU/binfmt emulation on an amd64 host. Docker Desktop generally provides this; a Linux Docker host needs binfmt configured by its administrator. The demo does not install privileged emulation handlers.

From the **project root**:

```bash
export AK_API=https://localhost
read -rsp 'Artifact Keeper token: ' ARTIFACT_KEEPER_TOKEN; echo
export ARTIFACT_KEEPER_TOKEN
```

Obtain a token through your server's normal account/token workflow. `.env` is **not loaded automatically**. Never put tokens in source files or URLs. The script does not log or save them. Plain HTTP is supported for isolated local Docker networks; use HTTPS for remote/private clients. Redirects are refused to prevent forwarding credentials to another endpoint.

For a locally issued HTTPS certificate, add `--ca-bundle /path/to/root-ca.crt` to seeder commands. System CA roots are retained. This workstation has a local CA at `.local/p2-caddy-current-root.crt`; confirm it still belongs to the server. An expired server certificate must be renewed; adding a CA cannot make it valid.

## 1. Build sample packages

```bash
python3 seeders/debian/build-demo.py --versions 1.0.0-1
```

This creates four packages under `.local/apt-local-devtools/packages/` (ignored by Git):

| Package | Architectures | Installed command |
| --- | --- | --- |
| `ak-devtools-hello` | amd64, arm64 | `devtools-hello` |
| `ak-devtools-info` | amd64, arm64 | `devtools-info` |

Both commands print their package version, packaged architecture and runtime machine architecture. The payloads are portable shell scripts, deliberately packaged separately as amd64 and arm64 to exercise APT's architecture selection. **They are not compiled native binaries.** Each depends on `dash`, already installed in the Ubuntu base images. Native application packages need real target builds/cross-compilation and proper runtime dependencies.

The builder produces valid deterministic `ar` packages with gzip-compressed control/data archives and root ownership, without using root or `dpkg-deb`. It accepts `--output`, `--versions` (one or more), and `--architectures amd64 arm64`. Identical inputs reproduce identical bytes. Publish a new version when contents change.

## 2. Create, sign, upload and export the key

```bash
python3 seeders/debian/seed-debian.py \
  --create-repo \
  --directory .local/apt-local-devtools/packages \
  --export-key .local/apt-local-devtools/repo-key.asc
```

`--create-repo` creates a public local Debian repository if missing, creates a repository-bound GPG RSA-4096 key, and enables metadata signing. The server retains the private key; only the public key is exported. Existing matching repositories and active GPG configurations are reused. Existing visibility is preserved. An existing incompatible/disabled signing key is never silently replaced; resolve it in the server's signing settings. Repository/key creation involves multiple API calls; failures may leave a repository or an unattached key for administrator inspection. Avoid concurrent initialization.

Creation-only is supported:

```bash
python3 seeders/debian/seed-debian.py --create-repo
```

Upload existing packages without administrator creation privileges:

```bash
python3 seeders/debian/seed-debian.py --deb /path/to/example_2.0-1_amd64.deb

python3 seeders/debian/seed-debian.py \
  --deb /path/to/example_2.0-1_amd64.deb \
  --deb /path/to/example_2.0-1_arm64.deb

python3 seeders/debian/seed-debian.py --directory /path/to/debs
```

`--deb` and `--directory` are mutually exclusive. Directory mode recursively finds `*.deb`. The canonical filename and pool path come from **Package, Version and Architecture inside the control archive**, not the input filename. Package files are read into memory; size the seeding machine for your largest package. `--component` defaults to `main`; configuring another component requires the matching APT source entry.

| Option | Default / meaning |
| --- | --- |
| `--artifact-keeper` | `AK_API`, otherwise `https://localhost`; server base URL |
| `--repo` | `apt-local-devtools` |
| `--component` | `main` |
| `--deb FILE` | One existing file; repeatable |
| `--directory DIR` | Recursive `.deb` input |
| `--create-repo` | Create repository and initialize signing if needed |
| `--private` | Create with authenticated reads; requires `--create-repo` |
| `--ca-bundle FILE` | Add PEM CA roots to system trust |
| `--export-key FILE` | Export configured public signing key; also works without upload input |
| `--http-timeout SECONDS` | Per-request timeout, default 60 |

Existing files with the same SHA-256 are successful skips. Different bytes at the same canonical path are errors; never overwrite a released version. A concurrent HTTP 409 is skipped only after confirming identical bytes. Every upload is downloaded back and checked. HTTP 429 is retried up to four times with bounded numeric `Retry-After`; other HTTP failures are reported. The script does not automatically retry ambiguous uploads after network failures; rerunning safely checks existing bytes.

```text
============================================================
Summary
============================================================
Total:    4
Uploaded: 4
Skipped:  0
Failed:   0
```

Exit code **0** includes all-skipped runs. A validation/setup/upload error exits **1**; successfully uploaded files are retained when another upload fails. Repeating the same command retries safely.

## 3. Start Ubuntu amd64 and arm64 consumers

The supplied Compose file connects both clients to the existing `artifact-keeper-network` network. It expects the backend at `http://artifact-keeper-backend:8080` on that network and the exported key at `.local/apt-local-devtools/repo-key.asc`. No admin credentials are passed into either client. Both use Ubuntu **24.04 LTS**.

```bash
docker compose -f seeders/debian/compose.yml up -d --wait --wait-timeout 180
docker compose -f seeders/debian/compose.yml logs --no-color

docker compose -f seeders/debian/compose.yml exec ubuntu-amd64 devtools-hello
docker compose -f seeders/debian/compose.yml exec ubuntu-arm64 devtools-hello

docker compose -f seeders/debian/compose.yml exec ubuntu-amd64 \
  dpkg-query -W ak-devtools-hello ak-devtools-info
docker compose -f seeders/debian/compose.yml exec ubuntu-arm64 \
  dpkg-query -W ak-devtools-hello ak-devtools-info
```

The clients install **exactly `1.0.0-1`** by default, even if `1.1.0-1` is already published. The startup script writes a deb822 `.sources` file, installs the dedicated public key, refreshes only this source, installs both packages, and leaves the containers running. A health check confirms both packages and commands exist. Images need to be downloaded first if absent; installation itself only needs the local repository because `dash` is in the base image. Normal Ubuntu sources remain configured for future dependency installation.

Compose overrides:

```bash
APT_DOCKER_NETWORK=your-existing-network \
APT_REPO_URL=http://your-backend:8080/debian/apt-local-devtools \
APT_REPO_KEY_FILE=/absolute/path/to/repo-key.asc \
APT_PACKAGE_VERSION=1.1.0-1 \
docker compose -f seeders/debian/compose.yml up -d --wait
```

Relative bind paths resolve against `seeders/debian/`. Prefer an absolute key override. `localhost` **inside a container means that container**, not the host/server. Use the backend service name on its Docker network or a reachable DNS name. For HTTPS with a private CA, provision `ca-certificates` and your CA in the image before startup, as described below; the demo's internal HTTP default does not need CA provisioning. Private repositories additionally require read credentials provisioned as a secret; the included public-read demo does not mount credentials.

Remove the demo clients when finished (repository/packages remain on the server):

```bash
docker compose -f seeders/debian/compose.yml down
```

## 4. Configure any Ubuntu/Debian VM or container

Use a hostname reachable **from that client**, with the correct HTTPS certificate. Keep Ubuntu's normal sources for OS/runtime dependencies; adding this source does not replace them or make upstream packages available offline.

### TLS trust and signing-key trust

These are separate: the TLS CA verifies the server connection; the repository key verifies signed APT metadata and the package checksums referenced by it.

Install normal client tools through an already trusted source:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
```

If the server uses a private TLS CA, obtain the CA through your administrator, install it and update system trust:

```bash
sudo install -m 0644 /path/to/artifact-keeper-root-ca.crt \
  /usr/local/share/ca-certificates/artifact-keeper-root-ca.crt
sudo update-ca-certificates
```

Obtain `repo-key.asc` from the seeder's `--export-key` output via a trusted channel. Alternatively, for public repositories, download it from the HTTPS endpoint after establishing CA trust:

```bash
curl --fail --show-error --silent \
  https://packages.example.com/debian/apt-local-devtools/gpg-key.asc \
  --output repo-key.asc
gpg --show-keys --with-fingerprint repo-key.asc
```

Compare its full fingerprint with the administrator's trusted fingerprint (the seeder prints it), then install it:

```bash
sudo install -d -m 0755 /etc/apt/keyrings
sudo install -m 0644 repo-key.asc /etc/apt/keyrings/apt-local-devtools.asc
```

Armored keys use `.asc`; binary/dearmored keys use `.gpg`. Keep keys readable by `_apt` (0644). Use `Signed-By` to bind this repository to its key. Do not use `apt-key`, `trusted=yes`, `--allow-unauthenticated`, or disabled HTTPS checks. See [APT source configuration](https://manpages.ubuntu.com/manpages/noble/man5/sources.list.5.html) and [APT archive authentication](https://manpages.ubuntu.com/manpages/jammy/man8/apt-secure.8.html).

### Add the update site

Run on the VM/container, replacing `packages.example.com`:

```bash
arch=$(dpkg --print-architecture)
sudo tee /etc/apt/sources.list.d/apt-local-devtools.sources >/dev/null <<EOF
Types: deb
URIs: https://packages.example.com/debian/apt-local-devtools
Suites: stable
Components: main
Architectures: $arch
Signed-By: /etc/apt/keyrings/apt-local-devtools.asc
EOF

sudo apt-get -o APT::Update::Error-Mode=any update
apt-cache policy ak-devtools-hello ak-devtools-info
sudo apt-get install ak-devtools-hello ak-devtools-info
devtools-hello
```

Legacy one-line equivalent, **use either this or the deb822 file, not both**:

```text
deb [arch=amd64 signed-by=/etc/apt/keyrings/apt-local-devtools.asc] https://packages.example.com/debian/apt-local-devtools stable main
```

Use `arch=arm64` on arm64. Installing a foreign-architecture executable on an amd64 machine is a separate multiarch/emulation task; setting the source architecture alone does not make it runnable.

### Private repository reads

Create a new private repository using `--create-repo --private --repo YOUR-KEY`, or manage existing visibility in Artifact Keeper. Public signing keys do not grant read permission to private packages. Give consumers a dedicated **read-only** credential, separate from the seeder/admin token.

For APT's Basic-auth transport, put credentials in a root-owned mode-0600 `/etc/apt/auth.conf.d/apt-local-devtools.conf`:

```text
machine https://packages.example.com/debian/apt-local-devtools/
login YOUR_USERNAME
password YOUR_READ_ONLY_PASSWORD
```

The server's normal Basic-auth account flow is supported. If using a scoped API token, verify your deployed server's token-as-password convention before substituting it. Do not place credentials in the world-readable `.sources` file. For internal HTTP, the `machine` must explicitly start with `http://`; without a scheme APT sends credentials only over HTTPS/tor+HTTPS. See [apt_auth.conf](https://manpages.ubuntu.com/manpages/noble/man5/apt_auth.conf.5.html). Download a private repo's key with authentication or distribute the exported key through your administrator.

## 5. Publish a newer version and verify upgrade behavior

**`apt update` does not upgrade installed packages.** It downloads repository metadata. APT then selects candidates using Debian version ordering, architecture, dependencies, pin priorities and holds. New packages are not automatically installed either. Uploading a newer version to Artifact Keeper does not push it to clients.

Publish four `1.1.0-1` packages while keeping the original files:

```bash
python3 seeders/debian/build-demo.py --versions 1.1.0-1
python3 seeders/debian/seed-debian.py --directory .local/apt-local-devtools/packages
```

This skips the four original archives and uploads the four new archives. Verify on both running demo clients:

```bash
docker compose -f seeders/debian/compose.yml exec -T ubuntu-amd64 sh /demo/upgrade-client.sh
docker compose -f seeders/debian/compose.yml exec -T ubuntu-arm64 sh /demo/upgrade-client.sh
```

The verification script records the installed version, refreshes the local source, asserts that **update alone changed nothing**, shows installed/candidate versions, then upgrades only the two demo packages and checks for `1.1.0-1`. Override `EXPECTED_VERSION` for later releases. Restarting/recreating the clients reruns the exact-version startup install; set `APT_PACKAGE_VERSION=1.1.0-1` if recreating an already upgraded demo.

For a regular client:

```bash
sudo apt update
apt list --upgradable

# Upgrade only these already-installed development tools:
sudo apt-get install --only-upgrade ak-devtools-hello ak-devtools-info

# Or upgrade eligible installed packages across ALL configured repositories:
sudo apt upgrade
```

`apt upgrade` can add dependencies but does not remove existing packages. `apt full-upgrade` may remove packages to resolve dependency changes; review the proposed changes. Holds (`apt-mark hold`), pinning, dependency conflicts and phased updates can prevent individual upgrades. Automatic updates require an explicitly configured scheduler such as `unattended-upgrades`, with this repository's origin allowed; merely adding a source does not opt it into automatic upgrades. See the [Ubuntu apt manual](https://manpages.ubuntu.com/manpages/noble/man8/apt.8.html).

## Troubleshooting and validation

- **404 / package not found:** use `/debian/apt-local-devtools`, `stable`, `main`, and the correct architecture; run `apt update` after publication and inspect `apt-cache policy`. Missing dependencies must come from Ubuntu or be uploaded separately.
- **NO_PUBKEY / bad signature:** check `Signed-By`, permissions and the full fingerprint. After legitimate signing-key rotation, distribute the new trusted public key and refresh metadata.
- **TLS certificate expired/unknown issuer:** renew the server certificate or install the correct CA; check client time. Never fix this by disabling verification.
- **401/403:** check token expiry and repository permissions, private consumer credentials, or guest-read policy. Creation/signing needs administrator permissions.
- **Checksum conflict:** increase the Debian version; the published archive is immutable. Do not rebuild a released version with different bytes.
- **Zstd errors:** install `dpkg-deb` on the uploader or build gzip/xz packages; check server compression support too.
- **ARM exec format error:** use a native ARM host or administrator-configured binfmt/QEMU support. The demo does not change host emulation settings.
- **Other APT sites fail during update:** check the new site's output independently using the source-isolation options in `install-client.sh`; an unrelated Ubuntu mirror problem does not imply this repository failed.

```bash
python3 -m unittest discover -s seeders/debian -p 'test_*.py'
python3 seeders/debian/seed-debian.py --help
sh -n seeders/debian/install-client.sh seeders/debian/upgrade-client.sh
```

Generated packages, exported keys and local execution records belong under `.local/apt-local-devtools/`. Source files contain no credentials or private keys. The public repository is a development sample, not a replacement for Ubuntu's security repositories.

## Verified local run — 2026-11-09

The repository was actually created and seeded against the running local server:

- **8 archives published:** two package names × two architectures × versions `1.0.0-1` and `1.1.0-1`.
- **GPG fingerprint:** `F49B0E065E5229AAD3E2C506E3BCB1E0F2C1F10A`. This identifies this local run; a fresh server/repository generates its own key.
- **Ubuntu 24.04 containers:** `apt-devtools-ubuntu-amd64-1` and `apt-devtools-ubuntu-arm64-1`, both left running and healthy.
- Both clients verified signed APT metadata and installed `1.0.0-1`. After publishing `1.1.0-1`, metadata refresh left the installed version at `1.0.0-1`, with candidate `1.1.0-1`. Targeted upgrades installed and ran `1.1.0-1` on both clients (`x86_64` / `aarch64`).
- A full seeder rerun reused the signing key and reported **0 uploaded, 8 skipped, 0 failed**, exit 0.
- Ten Python regression tests passed; Python compilation and shell syntax checks passed.

The local HTTPS proxy's certificate was expired at test time. The run used the backend's internal Docker-network HTTP endpoint, including host-side seeding against its current container IP. No TLS checks were disabled. The Compose clients use the stable backend DNS name instead of that temporary IP. Renew the HTTPS certificate before using `https://localhost` from the host or configuring remote HTTPS clients.
