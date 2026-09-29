# Cargo seeder

`seed-cargo.py` mirrors crates.io dependencies into Artifact Keeper and publishes internal Rust crates. It uses Cargo to resolve dependencies and preserves the original `.crate` archives and their SHA-256 checksums. The Artifact Keeper server does not need Internet access; the machine running the seeder needs access to crates.io and the server.

## Repository layout

| Repository | Type | Purpose |
| --- | --- | --- |
| `cargo-external` | Local / Cargo | Original crates.io packages and their dependencies |
| `cargo-internal` | Local / Cargo | Your own packages, published with Cargo |
| `cargo` | Virtual / Cargo | Consumer reads across both local repositories |

Configure both local repositories as members of `cargo`. Publish to a local repository; use the virtual repository for dependency resolution. Avoid publishing different packages with the same name/version into different members.

## Requirements and configuration

- Python **3.11+**, using only the standard library (`tomllib` is required).
- A working Rust/Cargo toolchain and native linker. Project mode performs Cargo's normal package/build verification. Install the required compiler/targets before working offline.
- A token with repository read and artifact-write access to the destination repositories.

From the project root:

```bash
export ARTIFACT_KEEPER_TOKEN='<your token>'
export AK_API=https://localhost
```

`AK_API` is the **server base URL**, without `/cargo/...` or `/api/v1/...`. A `.env` file is not loaded automatically. `--artifact-keeper` overrides `AK_API`; the default is `https://localhost`.

For a local Caddy certificate, add this to the commands below:

```bash
--ca-bundle /path/to/current-caddy-root.crt
```

On this workstation the current CA is `.local/p2-caddy-current-root.crt`. Python retains the system trust roots and adds the supplied CA. The script also builds a combined CA bundle for Cargo, so both crates.io and the local server can be verified. TLS verification remains enabled.

Optional repository-key overrides are `--external-repo`, `--internal-repo` and `--virtual-repo`. Cargo's registry aliases remain `cargo-external`, `cargo-internal` and `cargo`, respectively. `--cargo` selects the Cargo executable.

## 1. Mirror dependencies from TOML

```bash
python3 seeders/cargo/seed-cargo.py \
  --toml-file /path/to/project/Cargo.toml \
  --ca-bundle .local/p2-caddy-current-root.crt
```

A full Cargo project or workspace manifest must be named `Cargo.toml`. The seeder runs `cargo fetch`, without restricting the target architecture, then mirrors every crates.io package in the workspace's `Cargo.lock`. This covers direct and transitive normal/build/dev dependencies and the optional/target-specific packages resolved into that lockfile. Workspace/path packages are not uploaded as external dependencies.

- An existing `Cargo.lock` is respected using `--locked`. A stale lock fails rather than silently changing versions.
- If the lock is missing, Cargo creates it **in the source workspace**. Keep it for reproducible consumer builds.
- The seeder does not build or execute the downloaded dependencies in external modes.
- Dependencies already originating from the configured Artifact Keeper registries are fetched for resolution but are not copied into `cargo-external`. Publish internal crates first if the manifest refers to them.

A dependencies-only file is also supported, for example `dependencies.toml`:

```toml
[dependencies]
serde = { version = "1", features = ["derive"] }
serde_json = "1"
```

The script resolves this fragment in a temporary wrapper crate. Dependency, dev-dependency, build-dependency, target and feature tables are allowed. Path dependencies and workspace inheritance require a complete Cargo project, not a fragment.

## 2. Mirror one dependency and its dependency graph

```bash
python3 seeders/cargo/seed-cargo.py --dependency serde \
  --ca-bundle .local/p2-caddy-current-root.crt

python3 seeders/cargo/seed-cargo.py --dependency serde@1.0.229 \
  --ca-bundle .local/p2-caddy-current-root.crt
```

A name without a version lets Cargo resolve a compatible non-yanked version for the active toolchain. `name@version` is translated to an **exact** Cargo requirement (`=version`), not a caret range. The version must actually exist on crates.io; `serde@1.0.288`, for example, is syntax the script accepts but its availability must be checked by Cargo. Use `--toml-file` when you need feature selection, renamed dependencies or version ranges.

## 3. Publish an internal crate

In your internal crate's `Cargo.toml`, restrict the allowed destination:

```toml
[package]
name = "your-internal-crate"
version = "0.1.0"
publish = ["cargo-internal"]
```

Then run:

```bash
python3 seeders/cargo/seed-cargo.py \
  --project-dir /path/to/internal-crate \
  --ca-bundle .local/p2-caddy-current-root.crt
```

The script validates the configuration and manifest, checks the internal sparse index for `<name>@<version>`, and only then executes:

```text
cargo publish --registry cargo-internal --manifest-path ... --package ...
```

Cargo's package/build verification is enabled; the script does not use `--no-verify`. Existing project lockfiles use `--locked`. Dependencies must already be available through Artifact Keeper; project mode does not mirror them automatically.

Use `--package <workspace-member>` to select a crate from a virtual workspace. Use `--allow-dirty` only when you intentionally want Cargo to package uncommitted files, as in the newly created example. Dependencies with `path` plus a published version must be published in dependency order.

## Already-published versions and failures

Existing versions print a warning and are **successful skips**, including an internal crate whose source directory has since changed. Increment its version to publish changes. External duplicates must have the same checksum as crates.io; a conflicting checksum is an error and is never overwritten.

Uploads are verified against the index; external archives are also downloaded and compared to the upstream checksum. A matching concurrent external publication is a skip. Project mode also rechecks the index if Cargo fails after publishing or races another publisher.

Output follows the PyPI seeder's format:

```text
============================================================
Summary
============================================================
Total:    7
Uploaded: 0
Skipped:  7
Failed:   0
```

A run containing only skips exits **0**. Actual validation, resolution, authentication, checksum or upload failures exit **1**. HTTP 429 responses handled by Python honor numeric `Retry-After` values with bounded retries; errors from Cargo itself retain Cargo's own retry behavior.

## Consumer configuration: keep external dependencies unchanged

**Your external dependency declarations in `Cargo.toml` can remain unchanged.** Configure source replacement once on each VM in `~/.cargo/config.toml` (or `$CARGO_HOME/config.toml` if you use a custom Cargo home). Merge the following with any existing configuration:

To apply this only to one project, use that project's `.cargo/config.toml` instead:

```toml
[source.crates-io]
replace-with = "cargo"

[registries.cargo]
index = "sparse+https://localhost/cargo/cargo/"

[registries.cargo-internal]
index = "sparse+https://localhost/cargo/cargo-internal/"

[registry]
global-credential-providers = ["cargo:token"]
```

The `sparse+` prefix and trailing `/` matter. Standard public dependencies retain their crates.io identity and lockfile checksums, while source replacement downloads their identical mirrored bytes through `cargo`. Declare private dependencies explicitly as alternate-registry dependencies:

```toml
[dependencies]
serde = "1"
serde_json = "1"
your-internal-crate = { version = "0.1", registry = "cargo" }
```

Only private dependencies need `registry = "cargo"`. Existing external version requirements, features and renamed dependencies can remain unchanged.

The spelling is **`crates-io`**, plural. `replace-with = "cargo"` directs crates.io dependency requests **to Artifact Keeper**, not to the Internet. Missing mirrored packages cause an error; Cargo does **not** fall back to crates.io for this replaced source. Git dependencies and dependencies explicitly naming other registries are outside this replacement.

With user-wide configuration, individual projects need no registry configuration file unless they override these settings. Replace `localhost` with the Artifact Keeper VM's reachable hostname when the client runs on another machine.

External packages retain their crates.io source identities in `Cargo.lock`. Those URLs do not mean Cargo downloads from the Internet: source replacement supplies the identical archives from Artifact Keeper. Keep an existing crates.io lockfile and use `--locked` when its versions are mirrored with matching checksums. If migrating from explicit `registry = "cargo"` declarations on external dependencies, remove those fields and deliberately regenerate the lockfile once because the logical source identities change. Keep the registry field on private crates.

An environment variable can override the named registry's URL:

```bash
export CARGO_REGISTRIES_CARGO_INDEX='sparse+https://localhost/cargo/cargo/'
```

**This variable alone is not enough to redirect ordinary external dependencies.** Keep `[source.crates-io] replace-with = "cargo"` in Cargo configuration. With that rule, public dependencies stay unchanged and use the exported URL. Add the export to your shell profile if you want it to persist; Cargo does not interpolate `${VARIABLE}` inside TOML. `CARGO_REGISTRY_DEFAULT=cargo` also does not replace crates.io dependency routing.

Run external seeding on the Internet-connected staging machine. You can pass the unchanged project's `Cargo.toml` to `--toml-file`; publish any private dependencies first so Cargo can resolve them. The seeder deliberately isolates Cargo configuration to fetch upstream crates instead of reading them back from the mirror. Its destination is configured with `AK_API`/`--artifact-keeper`, independently of consumer environment overrides.

Run Cargo **from the consumer project directory** so its `.cargo/config.toml` is discovered. If local TLS trust is needed, set `CARGO_HTTP_CAINFO` to a trusted PEM bundle. For private virtual repositories, also set `CARGO_REGISTRIES_CARGO_TOKEN`; use `CARGO_REGISTRIES_CARGO_INTERNAL_TOKEN` for direct `cargo publish`. The seeder sets registry-specific credential environment variables itself and never writes tokens into configuration files.

## Isolation and scope

Each seeder run uses a temporary Cargo home, cache, target directory and explicit registry configuration. Project/global Cargo configuration is deliberately ignored: otherwise a consumer's source replacement would try to fetch missing upstream crates from the empty mirror. Custom registry aliases, patches declared only in Cargo configuration, custom linkers and other config-only settings are not imported. Manifest/workspace patches still follow Cargo's behavior. Keep `--work-dir` outside trees containing ancestor `.cargo` configuration.

Git and unrelated third-party registry dependencies are rejected as mirror sources. Path/workspace crates are not registry packages and must be published separately when consumers need them. Yanked upstream versions are refused because this server's publish endpoint does not preserve upstream yank status. New index schema versions beyond v2 are rejected. v2 feature definitions are merged into the feature map consumed by Artifact Keeper's current Cargo handler, so use a modern Cargo client.

The current backend emits the optional minimum-Rust-version hint as `rust-version`, rather than Cargo's `rust_version`; this seeder cannot correct that server-generated field. Keep consumer lockfiles and use a compatible Rust toolchain rather than relying on registry MSRV filtering. Original archive checksums, dependency definitions and feature definitions are preserved.

A seeded registry supplies Rust crate sources. System libraries, compiler targets, Git repositories, and downloads performed by build scripts may still need separate preparation. An offline **server** does not imply an offline Cargo client: clients still need network access to Artifact Keeper. `cargo --offline` works only after that client's own cache has been populated.

## Example and tests

The complete example is [examples/rust-proj](../../examples/rust-proj/README.md). It publishes an internal greeter, seeds the application's external dependencies, then builds through the virtual repository with no path dependency on the greeter source.

```bash
python3 -m unittest discover -s seeders/cargo -p 'test_*.py'
python3 seeders/cargo/seed-cargo.py --help
```

References: [Cargo registries](https://doc.rust-lang.org/cargo/reference/registries.html), [source replacement](https://doc.rust-lang.org/cargo/reference/source-replacement.html), [cargo fetch](https://doc.rust-lang.org/cargo/commands/cargo-fetch.html), [cargo publish](https://doc.rust-lang.org/cargo/commands/cargo-publish.html).
