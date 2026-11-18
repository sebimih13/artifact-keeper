# Upload a local Eclipse P2 update site

`seed-p2.py` uploads an already built update site to a **P2 Local** repository.
It preserves every file, including categories, dependencies and compressed
metadata. It does not build EGit or regenerate P2 metadata. Python 3.9+ with the
standard library is sufficient; Eclipse is not required for uploading.

Create a local repository with format `p2` in Artifact Keeper, then run:

```bash
export AK_API_TOKEN='<token with repository read/write access>'
python3 seeders/p2/seed-p2.py \
  --dir ~/Developer/egit/org.eclipse.egit.repository/target/repository \
  --artifact-keeper https://localhost/api/v1/repositories/egit-local
```

Only `--dir` and `--artifact-keeper` are required. The URL must be the full
repository API URL, without `/download/`. `ARTIFACT_KEEPER_TOKEN` is accepted
as a fallback for existing environments.

The directory must contain `content` and `artifacts` metadata in `.jar`, `.xml`
or `.xml.xz` form. All files below it are uploaded with their relative paths;
symlinks are rejected. Supply only the built update-site directory, not the
project source tree or a directory containing private build files.

The uploader uses the chunked upload API and supplies bundle/feature names and
versions from `plugins/<id>_<version>.jar` and `features/<id>_<version>.jar`, so
they appear correctly in Artifact Keeper's package list. It compares remote
SHA-256 checksums to skip unchanged files, rejects changes to published bundle
or feature JARs, and uploads payloads before metadata, with `p2.index` last.
Each upload's checksum and downloaded bytes are verified. Rate limits are
retried up to four times; other errors stop remaining uploads and exit nonzero.

Use the printed update-site URL in Eclipse's **Help → Install New Software…**:

```text
https://localhost/api/v1/repositories/egit-local/download/
```

For a private repository, Eclipse needs its own authentication. The script's
token is not passed to Eclipse. Dependencies outside the site must be available
through the appropriate Eclipse update sites.

## TLS

Certificate verification uses Python's system trust store. For a local CA,
set `SSL_CERT_FILE` once in your environment, or supply `--ca-bundle <PEM>`.
On this workstation the current local Caddy CA is available at:

```bash
export SSL_CERT_FILE="$PWD/.local/p2-caddy-current-root.crt"
```

Eclipse needs that CA in its Java trust store as well. This workstation uses
`.local/eclipse-cacerts`; Python's CA settings do not configure Java.

## Updating a site

Rebuild locally and rerun the same command. Changed plugin/feature contents
must have new versions. Remote files absent from the local build are not
deleted. Publish the complete site whose metadata you want clients to see.

Serialize writers: replacing multiple metadata files is not atomic, so clients
can briefly see mixed generations during an update. Use a separate repository
for an immutable release if that matters. Composite sites and nonstandard
artifact mappings are not validated or resolved by this uploader.

## Tests

```bash
python3 -m unittest discover -s seeders/p2 -p 'test_*.py'
```

Tests use a temporary HTTP server to exercise the two-argument CLI, chunked
uploads, package coordinates, repeat-run skips, immutable JARs, upload order,
rate-limit retries, failure handling, metadata preservation and symlink rejection.
