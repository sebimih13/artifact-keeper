# Runnable local p2 seeding example: Eclipse 2026-12

This is the executable companion to [SEED-LOCAL.md](SEED-LOCAL.md). It uses the
same source and local p2 target, with parallel staging, resumable uploads, and
verification of every downloaded file. The example was executed successfully on 20 October 2026; its results are
recorded below.

## Scripts

| File | Role |
| --- | --- |
| [`seed-local.py`](seed-local.py) | Prepare isolated Eclipse state, invoke Ant, validate the mirror, create the private local repository, upload, verify, and optionally publish |
| [`stage_upstream.py`](stage_upstream.py) | Resolve upstream composites and stage their metadata/artifacts with up to 12 concurrent downloads |
| [`build.xml`](build.xml) | Run Eclipse's `p2.mirror` task to assemble the final flat site |
| [`test_seed_local.py`](test_seed_local.py) | Check corrupt/missing input rejection, existing-repository protection, and publication withheld on failed verification |

Python 3.11+ with its standard library is required. The defaults use the existing
Eclipse 2026-09 installation, Java 25, and the dedicated local Caddy truststore.
Run commands from `/home/seb/Developer/artifact-keeper`. No Python dependencies
need installing.

## Run the example

First ensure the certificate files described in
[the certificate setup](README.md#https-certificates) exist:

```bash
test -f .local/p2-tls/caddy-root.crt
test -f .local/p2-tls/cacerts
```

Mirror and validate the upstream site without API credentials:

```bash
python3 examples/p2-mirror/seed-local.py \
  --phase mirror --stage-upstream \
  --source https://download.eclipse.org/releases/2026-12/ \
  --work .local/p2-seed/2026-12-example
```

Then provide your API token through the environment and seed the local target:

```bash
read -r -s -p 'Artifact Keeper API token: ' API_TOKEN
printf '\n'
export API_TOKEN

python3 examples/p2-mirror/seed-local.py \
  --phase seed \
  --work .local/p2-seed/2026-12-example \
  --repository p2-eclipse-2026-12-local \
  --url https://localhost \
  --publish

unset API_TOKEN
```

The token needs repository creation, artifact uploads/reads, and repository
visibility management permissions. It is not written to the work directory or
logs. Omitting `--publish` leaves a newly created repository private. `--phase
all --stage-upstream` runs both phases in one invocation when `API_TOKEN` is
already set. `--workers` controls upload/download verification concurrency
(default 4, maximum 8); the staging downloader uses 12 workers.

Stop if a command exits nonzero. Logs and partial downloads are retained for
diagnosis. Retrying with the same arguments and work directory reuses the
resolved snapshot and matching completed files. Use a new work directory and
repository key to capture a later snapshot.

## What the scripts do

1. Resolve the upstream metadata and artifact composites using the original
   upstream URLs. Stage each dated leaf separately and record its source URL.
2. Download every advertised leaf artifact in parallel. Check declared sizes
   and available SHA-256/SHA-512/MD5 checksums. A local SHA-256 completion record
   supports safe reuse even when upstream omitted checksums. Temporary `.part`
   files are never treated as completed downloads.
3. Write staging composite catalogs whose children are local `file:` URLs.
   These are only an input to Ant; they are never uploaded to Artifact Keeper.
4. Invoke `org.eclipse.ant.core.antRunner` using an isolated configuration and
   workspace. Ant mirrors the staged composite into a flat `site/` directory.
   External mirror selection is disabled. The original upstream metadata is
   retained semantically; plugin/feature descriptions are not regenerated.
5. Check that the final IU set equals the staged source IU set. Validate all
   final artifact files and available source checksums, and add regular-site
   `p2.index` discovery metadata. Staging completion sidecars stay outside the
   final mirror.
6. Create a fresh private repository with `format: p2` and `repo_type: local`.
   The script never adopts an unrelated existing repository. Its repository
   receipt records the server URL, key, and UUID for subsequent retries.
7. Upload through the chunked artifact API. Plugins/features have explicit
   package names and versions. Payloads finish before artifact metadata,
   content metadata, and finally `p2.index`. Existing matching SHA-256 files are
   skipped; existing different bytes are rejected instead of overwritten.
8. Compare the complete hosted inventory with the local SHA-256 manifest, then
   download **every file** through `/general/{key}/` and compare its size and
   SHA-256 with the mirror. Repository visibility is changed only after these
   checks succeed.
9. With `--publish`, enable anonymous reads and verify anonymous downloads of
   `p2.index`, `content.jar`, and `artifacts.jar`.

Uploads can be resumed after a failure using the repository receipt and server
inventory. Ambiguous failed mutations are not blindly retried; explicit HTTP
429 responses honor `Retry-After`. An interrupted upload session can remain on
the server until normal session cleanup, but a retry only skips completed
hosted files. Multi-file uploads are not atomic, so the repository starts
private. A previously published target is never updated with different files
by this script.

The staging helper intentionally supports the standard Eclipse
`plugins/${id}_${version}.jar`, `features/${id}_${version}.jar`, and
`binary/${id}_${version}` mappings. It rejects unsupported/noncanonical formats
and mismatched metadata/artifact child lists rather than guessing. For other
upstream layouts use a suitable custom staging plan or omit `--stage-upstream`
and let Eclipse access the upstream directly.

## Files produced

The work directory is `.local/p2-seed/2026-12-example/`, outside tracked source:

| Path | Contents |
| --- | --- |
| `upstream/plan.json` | Resolved source leaves, expected IU coordinates, and artifact download plan |
| `upstream/<URL hash>/` | Staged per-source p2 catalogs, payloads, and local completion sidecars |
| `configuration/`, `workspace/`, `p2-data/` | Isolated Eclipse runtime state |
| `mirror.log` | Eclipse Ant output, including dependency warnings and final build status |
| `mirror-complete.json` | Successful Ant completion and original source URL |
| `site/` | The flat update site uploaded to Artifact Keeper |
| `inventory.json` | Local unit/artifact counts and file SHA-256 manifest |
| `repository.json` | Target identity receipt; contains no token |
| `result.json` | Final upload, complete download verification, and publication result |
| `upload.log` | Saved console output from the actual seeding run |
| `eclipse-list.log` | Eclipse director listing of the published HTTPS update site |

Keep the receipt and manifest with the work directory if you want to resume or
verify this snapshot again. Updating the upstream source does not automatically
refresh the local repository.

## Use the result in Eclipse

In **Help → Install New Software → Add**, use:

```text
https://localhost/general/p2-eclipse-2026-12-local/
```

The existing `/general/*` Caddy route must be present. Eclipse's Java runtime
must trust the Caddy local CA; no client certificate is required. This pipeline
does not change the IDE's trust settings or export any private keys.

The server serves uploaded files from local storage without consulting the
upstream. No remote/helper/virtual repository is created by this example.
Missing external dependencies may still prevent installation of particular
EPP products. A complete file mirror is not a guarantee that every upstream
product can be installed offline. Inspect the Ant log, and test the desired
features with **Contact all update sites during install to find required
software** unchecked.

## Checks

```bash
python3 -m unittest discover -s examples/p2-mirror -p 'test_*.py'
```

The operational run additionally validates the real mirror, server inventory,
all served file checksums, and anonymous metadata access before reporting
success. It does not install software into the running IDE.


## Executed results — 20 October 2026

The run created `p2-eclipse-2026-12-local` as a **local p2 repository**, uploaded
all files privately, verified them, and enabled anonymous reads. Its upstream
snapshot selection was:

| Source leaf | Installable units | Artifact descriptors |
| --- | ---: | ---: |
| `https://download.eclipse.org/technology/epp/packages/2026-12/202610081000/` | 469 | 92 |
| `https://download.eclipse.org/releases/2026-12/202610091000/` | 5,525 | 4,828 |

The root URL was still a development catalog at this date. This example freezes
those resolved children; it does not claim to contain the final December release.

| Verification | Result |
| --- | --- |
| Eclipse Ant build | `BUILD SUCCESSFUL` |
| Final installable units | 5,994; exact match with the source unit set |
| Plugins | 4,278 |
| Features | 541 |
| Binary artifacts | 101 |
| Source artifact checksum verification | All 4,920 passed; none lacked source checksums |
| Uploaded site files | 4,923, including the three root metadata/discovery files |
| Total uploaded bytes | 1,817,531,720 |
| Server inventory comparison | Exact path and SHA-256 match |
| Full download verification | All 4,923 files matched size and SHA-256 |
| Anonymous access | `p2.index`, `content.jar`, and `artifacts.jar` verified |
| Eclipse p2 director | Loaded the published HTTPS URL and listed all 5,994 expected units |
| Offline script tests | Six passed |

The full response is in
`.local/p2-seed/2026-12-example/result.json`; the local file manifest is in
`inventory.json`. No existing September repositories were modified by this run.
No source composite overrides were uploaded to the new local repository.

The Ant log reports unresolved EPP package-root dependencies on
`org.eclipse.justj.epp.feature.group` version `25.0.4.v20260826-1347`, a separately
distributed runtime feature. All artifacts actually advertised by the two source
leaves are present, but complete offline EPP product provisioning may require
that additional runtime repository. No features were installed into the IDE.

For the final Eclipse-client check, the same Java/truststore and isolated
configuration were used with:

```bash
/usr/lib/jvm/java-25-openjdk/bin/java \
  -Djavax.net.ssl.trustStore="$PWD/.local/p2-tls/cacerts" \
  -Djavax.net.ssl.trustStorePassword=changeit \
  -Dorg.eclipse.ecf.provider.filetransfer.excludeContributors=org.eclipse.ecf.provider.filetransfer.httpclientjava \
  -jar /home/seb/eclipse/committers-2026-09/eclipse/plugins/org.eclipse.equinox.launcher_1.8.0.v20260804-1928.jar \
  -configuration "$PWD/.local/p2-seed/2026-12-example/configuration" \
  -data "$PWD/.local/p2-seed/2026-12-example/director-workspace" \
  -nosplash -consolelog \
  -application org.eclipse.equinox.p2.director \
  -repository https://localhost/general/p2-eclipse-2026-12-local/ \
  -list
```
