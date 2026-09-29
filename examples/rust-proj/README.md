# Rust example: external and internal crates through Artifact Keeper

This application uses:

- `serde` and `serde_json`, mirrored from crates.io into `cargo-external`.
- `ak-example-greeter`, published from `internal-greeter/` into `cargo-internal`.
- The virtual `cargo` repository for **all dependency downloads**.

The application declares the internal crate with `registry = "cargo"`, not a path dependency. Its test serializes the greeting using the external crates. The local internal source exists only for publishing and its own tests.

## Prepare the repositories

From the artifact-keeper project root, with Python 3.11+, Cargo/Rust and a linker installed:

```bash
export AK_API=https://localhost
export ARTIFACT_KEEPER_TOKEN='<your token>'
export AK_CARGO_CA="$PWD/.local/p2-caddy-current-root.crt"

# 1. The example internal crate has no external dependencies, so publish it first.
python3 seeders/cargo/seed-cargo.py \
  --project-dir examples/rust-proj/internal-greeter \
  --allow-dirty \
  --ca-bundle "$AK_CARGO_CA"

# 2. Resolve the consumer manifest and mirror its complete external lockfile graph.
python3 seeders/cargo/seed-cargo.py \
  --toml-file examples/rust-proj/Cargo.toml \
  --ca-bundle "$AK_CARGO_CA"

# 3. Exercise individual dependency mode; these should now be successful skips.
python3 seeders/cargo/seed-cargo.py --dependency serde \
  --ca-bundle "$AK_CARGO_CA"
python3 seeders/cargo/seed-cargo.py --dependency serde@1.0.229 \
  --ca-bundle "$AK_CARGO_CA"
```

Repeated project publication warns and skips without a build or failure. The seeder has its own isolated registry configuration; it does not read this example's Cargo config during upstream mirroring.

The checked-in `Cargo.lock` pins the versions validated here. It also contains the internal package's checksum for the package published during this example setup. Repackaging the same internal source on another machine can produce different archive bytes (for example due to VCS packaging metadata); in a newly provisioned registry, deliberately regenerate the example lockfile against that registry if its checksum differs. Never replace an already-published version's contents.

## Run using the virtual repository

```bash
cd examples/rust-proj
export CARGO_HTTP_CAINFO="$AK_CARGO_CA"
# Required only if the virtual repository denies anonymous downloads:
# export CARGO_REGISTRIES_CARGO_TOKEN="$ARTIFACT_KEEPER_TOKEN"

cargo test --locked
cargo run --locked
```

The output includes:

```json
{
  "greeting": "Hello, Rust, from cargo-internal!",
  "registry": "cargo (virtual)"
}
```

The `.cargo/config.toml` contains the sparse registry URL and replaces crates.io downloads with `cargo`. Rustup uses the example's stable toolchain rather than the backend project's pinned compiler. Install that toolchain before taking the build machine offline.

If your server is a separate VM, replace `localhost` in both `AK_API` and `.cargo/config.toml` with its reachable hostname. The internal registry URL is part of Cargo's source identity, so deliberately regenerate the lockfile when migrating this example to another registry URL.

## Verify with an empty cache and blocked public Cargo traffic

Keep access to the local server, but direct non-local proxy-aware traffic to a closed port:

```bash
export CARGO_HOME="$(mktemp -d /tmp/ak-rust-example-cache.XXXXXX)"
export CARGO_TARGET_DIR="$(mktemp -d /tmp/ak-rust-example-target.XXXXXX)"
export HTTP_PROXY=http://127.0.0.1:9
export HTTPS_PROXY=http://127.0.0.1:9
export ALL_PROXY=http://127.0.0.1:9
export NO_PROXY=localhost,127.0.0.1

cargo test --locked
cargo run --locked
cargo test --locked --offline
```

This is a proxy-based Cargo connectivity test, not an OS network sandbox. It was validated with a fresh cache: all 12 dependency crates were downloaded as `(registry cargo)`, including the internal greeter. After the cache is populated, the offline test also works.

During development here, a temporary Rust toolchain was installed without changing the shell profile. If it still exists, use it with:

```bash
export PATH="/tmp/ak-cargo-tools/bin:$PATH"
export RUSTUP_HOME=/tmp/ak-rustup
```

For long-term use, install Rust normally; temporary directories can be removed by the OS. See the [seeder README](../../seeders/cargo/README.md) for limitations and registry configuration.
