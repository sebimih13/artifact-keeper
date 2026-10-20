# Seed the December 2026 update site into a local repository

This procedure downloads an upstream p2 update site with Eclipse Ant, then
uploads the resulting flat site into a new **local p2 repository** in Artifact
Keeper. It does not create a remote repository or a virtual repository, and
it does not run `generate-composite.py`.

The steps below are instructions; the December mirror and repository have
not been created by writing this document. Run commands from the checkout root
in one Bash session. The full mirror can take several GB and many minutes.

## 1. Check the source and choose a snapshot

The source is:

```text
https://download.eclipse.org/releases/2026-12/
```

On 20 October 2026 its `compositeContent.jar` contained these child locations:

```xml
<child location="../../technology/epp/packages/2026-12/"/>
<child location="202610091000"/>
```

This is a moving development catalog at that date, not a guaranteed final
December release. The instructions mirror what it advertises when you run
them. Use a dated local repository name for successive snapshots rather than
replacing an existing published site. To mirror only the main snapshot,
excluding EPP package definitions, set `SOURCE` to the specific dated main
release child instead. Do not assume that child stays `202610091000`.

Eclipse resolves these relative children against the original upstream URL.
There is no Artifact Keeper proxy path involved during the mirror, so no EPP
helper remote or replacement remote metadata is needed.

## 2. Set paths and check prerequisites

Use an Eclipse installation with `org.eclipse.ant.core` and
`org.eclipse.equinox.p2.repository.tools`, and its supported Java runtime.
The paths below match the installation used for the September mirror:

```bash
export AK_ROOT="$PWD"
export AK_BASE=https://localhost
export AK_REPO=p2-eclipse-2026-12-local
export SOURCE=https://download.eclipse.org/releases/2026-12/
export ECLIPSE_HOME=/home/seb/eclipse/committers-2026-09/eclipse
export JAVA_BIN=/usr/lib/jvm/java-25-openjdk/bin/java
export AK_CA="$AK_ROOT/.local/p2-tls/caddy-root.crt"
export AK_TRUSTSTORE="$AK_ROOT/.local/p2-tls/cacerts"
export SEED_WORK="$AK_ROOT/.local/p2-seed/2026-12-$(date -u +%Y%m%dT%H%M%SZ)"
export SITE="$SEED_WORK/site"
mkdir -p "$SEED_WORK"

export LAUNCHER
LAUNCHER=$(find "$ECLIPSE_HOME/plugins" -maxdepth 1 \
  -name 'org.eclipse.equinox.launcher_*.jar' -print -quit)
test -n "$LAUNCHER" && test -f "$AK_CA" && test -f "$AK_TRUSTSTORE"
"$JAVA_BIN" -version
```

The CA and truststore paths refer to the persistent setup documented in
[README.md](README.md#https-certificates). Create them first if missing.
Python/curl need the PEM CA; Eclipse/Java need the Java truststore. For a
publicly trusted server certificate, use the normal system/Java trust instead.
No client certificate is required for these public update-site reads.

Confirm the deployment forwards `/general/*` to the backend; this setup uses
`artifact-keeper/docker/Caddyfile`. See the main README for the route and reload
commands. Use `/general/` for client downloads, avoiding the REST download
endpoint's separate rate limit. Use `/api/v1/` for provisioning and uploads.

## 3. Prepare an isolated Eclipse configuration

Do not share the running IDE's workspace or mutable configuration with the
headless job. This copies the installation's configured bundle list but keeps
runtime state and p2 data in the work directory:

```bash
python3 - <<'PY'
import os
import shutil
from pathlib import Path

home = Path(os.environ['ECLIPSE_HOME'])
work = Path(os.environ['SEED_WORK'])
config = work / 'configuration'
shutil.copytree(home / 'configuration', config,
                ignore=shutil.ignore_patterns('org.eclipse.osgi', '*.log'),
                dirs_exist_ok=True)
p = config / 'config.ini'
lines = [line for line in p.read_text().splitlines()
         if not line.startswith(('eclipse.p2.data.area=',
                                 'osgi.configuration.cascaded='))]
lines += ['eclipse.p2.data.area=' + (work / 'p2-data').as_uri() + '/',
          'osgi.configuration.cascaded=false']
p.write_text('\n'.join(lines) + '\n')
PY
```

## 4. Mirror the upstream site with Eclipse Ant

The existing `build.xml` accepts source and destination overrides. All JVM
properties must be before `-jar`; Ant properties are after the application
arguments:

```bash
"$JAVA_BIN" \
  -Djavax.net.ssl.trustStore="$AK_TRUSTSTORE" \
  -Djavax.net.ssl.trustStorePassword=changeit \
  -Declipse.p2.mirrors=false \
  -Dorg.eclipse.ecf.provider.filetransfer.excludeContributors=org.eclipse.ecf.provider.filetransfer.httpclientjava \
  -jar "$LAUNCHER" \
  -configuration "$SEED_WORK/configuration" \
  -data "$SEED_WORK/workspace" \
  -nosplash -consolelog \
  -application org.eclipse.ant.core.antRunner \
  -buildfile "$AK_ROOT/examples/p2-mirror/build.xml" \
  -Dsource="$SOURCE" \
  -Ddestination="$SITE" \
  > "$SEED_WORK/mirror.log" 2>&1
mirror_status=$?
tail -n 20 "$SEED_WORK/mirror.log"
test "$mirror_status" -eq 0
```

**Stop if the command failed.** Do not upload an incomplete mirror. Examine the
log for unresolved dependencies too: a successful artifact mirror does not
prove a complete EPP product can be installed offline. External Java-runtime
repositories or test dependencies may need separate mirroring for that goal.

With no IU filter, `p2.mirror` includes all advertised installable units and
mirrors their artifacts. The build disables repository-reference and source
repository-property copying. The output is a regular, flat repository with
`content.jar`, `artifacts.jar`, `plugins/`, `features/`, and possibly `binary/`.
It does not install anything into your IDE. Eclipse documents this task in
[Ant tasks for managing repositories](https://help.eclipse.org/latest/topic/org.eclipse.platform.doc.isv/guide/p2_repositorytasks.htm).

This mirrors existing p2 metadata. Do not run `seeders/p2/seed-p2.py` on the
result: that script republishes features/bundles from scratch and is a different
workflow, with a local Generic target and its own requirements.

## 5. Add discovery metadata and check the local site

The Ant mirror may omit `p2.index`; the upload helper requires it. Advertise
the regular XML/JAR repository factories, not the original composite factories:

```bash
python3 - <<'PY'
import os
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

site = Path(os.environ['SITE'])
for stem in ('content', 'artifacts'):
    with zipfile.ZipFile(site / (stem + '.jar')) as archive:
        root = ET.fromstring(archive.read(stem + '.xml'))
    assert 'Composite' not in root.get('type', ''), root.attrib
    print(stem, root.attrib)
(site / 'p2.index').write_text(
    'version=1\n'
    'metadata.repository.factory.order=content.xml,\\!\n'
    'artifact.repository.factory.order=artifacts.xml,\\!\n')
PY

python3 scripts/upload-p2.py "$SITE" \
  --repository "$AK_REPO" --dry-run
```

Check that `SITE` contains only site files: put logs and other run outputs in
`SEED_WORK`, outside `SITE`. The uploader recursively uploads every file there.

For an optional pre-upload Eclipse check, choose **Help → Install New Software
→ Add → Local** and select `SITE`. Verify the expected features are listed.

## 6. Supply an API token and create a fresh local p2 repository

The token must be allowed to create the repository, upload artifacts, read the
repository/package inventory, and update repository visibility. Public client
reads will not need this token after publication.

```bash
umask 077
export TOKEN_FILE
TOKEN_FILE=$(mktemp "$SEED_WORK/api-token.XXXXXX")
read -r -s -p 'Artifact Keeper API token: ' API_TOKEN
printf '\n'
printf '%s' "$API_TOKEN" > "$TOKEN_FILE"
unset API_TOKEN
trap 'rm -f "$TOKEN_FILE"' EXIT

python3 - <<'PY'
import json
import os
from pathlib import Path
Path(os.environ['SEED_WORK'], 'repository.json').write_text(json.dumps({
    'key': os.environ['AK_REPO'],
    'name': 'Eclipse 2026-12 local snapshot',
    'format': 'p2',
    'repo_type': 'local',
    'is_public': False,
}))
PY

curl --fail-with-body --cacert "$AK_CA" \
  -H "Authorization: Bearer $(cat "$TOKEN_FILE")" \
  -H 'Content-Type: application/json' \
  --data-binary "@$SEED_WORK/repository.json" \
  "$AK_BASE/api/v1/repositories"
```

**Stop if creation fails.** Use a fresh key if it already exists; do not delete
or overwrite an existing repository just to reuse this example name. The
repository stays private during seeding so clients cannot discover a partial
site. There is no `upstream_url` for a local repository.

## 7. Upload every mirrored site file

The existing uploader supports local p2 targets and supplies explicit package
coordinates for plugin/feature JARs. It uses chunked uploads with SHA-256
verification and then checks package indexing:

```bash
SSL_CERT_FILE="$AK_CA" python3 scripts/upload-p2.py "$SITE" \
  --repository "$AK_REPO" \
  --url "$AK_BASE" \
  --token-file "$TOKEN_FILE" \
  > "$SEED_WORK/upload.log" 2>&1
upload_status=$?
tail -n 20 "$SEED_WORK/upload.log"
test "$upload_status" -eq 0
```

**Stop on any failure; keep the repository private.** The current helper is
not an automatic resumable/idempotent publisher: rerunning against already
uploaded immutable coordinates may fail. Inspect the upload log and repository
inventory before recovery. It holds each file in memory, so allow RAM for the
largest artifact. It also assumes this upload's package coordinates are visible
to the supplied token during verification.

The helper prints a REST download URL at the end. For Eclipse use the native
`/general/` URL in step 9 instead.

## 8. Verify a download, then publish anonymous read access

For example, compare the uploaded content metadata with the mirrored file
while the repository is still private:

```bash
curl --fail-with-body --cacert "$AK_CA" \
  -H "Authorization: Bearer $(cat "$TOKEN_FILE")" \
  "$AK_BASE/general/$AK_REPO/content.jar" \
  -o "$SEED_WORK/uploaded-content.jar"
cmp "$SITE/content.jar" "$SEED_WORK/uploaded-content.jar"
```

Stop if the download or comparison fails. After upload verification succeeds,
allow anonymous reads:

```bash
curl --fail-with-body --cacert "$AK_CA" \
  -X PATCH \
  -H "Authorization: Bearer $(cat "$TOKEN_FILE")" \
  -H 'Content-Type: application/json' \
  --data '{"is_public":true}' \
  "$AK_BASE/api/v1/repositories/$AK_REPO"

curl --fail-with-body --cacert "$AK_CA" \
  "$AK_BASE/general/$AK_REPO/p2.index"

rm -f "$TOKEN_FILE"
trap - EXIT
```

If you require private client reads, keep `is_public` false and configure an
Eclipse-compatible authentication method separately. A Bearer token used by
the upload script is not automatically passed to Eclipse's p2 transport.

## 9. Install from the local Artifact Keeper repository

In **Help → Install New Software → Add**, enter:

```text
https://localhost/general/p2-eclipse-2026-12-local/
```

Eclipse must use the trusted Java configuration from the main README. Restart
the IDE after changing `eclipse.ini`. Uncheck **Contact all update sites during
install to find required software** to test dependency resolution using only
this site. If required units are missing, mirror and publish those dependencies
as well; do not treat disabling that option as proof of offline completeness.

A local Artifact Keeper repository serves the uploaded snapshot without going
to the upstream site. The seeding machine needs upstream network access for
step 4, but subsequent client downloads do not require that upstream.
No manual composite metadata is needed on this local target. For a later
snapshot, publish a new local repository and validate it before switching
clients or a separate composite site to the new URL.
