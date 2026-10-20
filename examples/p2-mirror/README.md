# Mirror Eclipse p2 through Artifact Keeper

For a complete local repository seeded from the December update site, see
[Seed a local p2 repository](SEED-LOCAL.md).

In Eclipse, open **Help → Install New Software → Add** and enter:

```text
https://localhost/general/p2/
```

The `p2` virtual repository has these three remote members, in order:

| Member | Upstream |
| --- | --- |
| `p2-eclipse-2026-09` | https://download.eclipse.org/releases/2026-09 |
| `p2-bash-editor` | https://de-jcup.github.io/update-site-eclipse-bash-editor/update-site |
| `p2-wildwebdeveloper` | https://download.eclipse.org/wildwebdeveloper/releases/1.4.0 |

The Eclipse release composite additionally needs its EPP child at
`https://download.eclipse.org/technology/epp/packages/2026-09`.
`p2-eclipse-epp-2026-09` proxies that child; it is referenced by the release
catalog and is not an additional member of the virtual repository.

This server's generic virtual resolution chooses the first matching path;
it does not merge p2 catalogs. Explicit `p2.index`, `compositeContent.xml/.jar`,
and `compositeArtifacts.xml/.jar` files were therefore published on `p2`.
The release member has an explicit composite too, to keep its relative EPP
child inside Artifact Keeper. The catalogs point at the native `/general/`
route, which was added to the deployment's Caddy routing. The REST
`/api/v1/repositories/{key}/download/` route has a separate 30-request/minute
limit and is unsuitable for bulk Eclipse downloads.

## Run the mirror

`build.xml` mirrors all advertised installable units and their artifacts,
including all feature/plugin versions and supported platforms. It does not
install them into your Eclipse IDE. The destination is a flat p2 update site.
Run with Java 25 for the installed Eclipse 2026-09:

```bash
/usr/lib/jvm/java-25-openjdk/bin/java \
  -Djavax.net.ssl.trustStore=/home/seb/Developer/artifact-keeper/.local/p2-tls/cacerts \
  -Djavax.net.ssl.trustStorePassword=changeit \
  -Declipse.p2.mirrors=false \
  -Dorg.eclipse.ecf.provider.filetransfer.excludeContributors=org.eclipse.ecf.provider.filetransfer.httpclientjava \
  -jar /home/seb/eclipse/committers-2026-09/eclipse/plugins/org.eclipse.equinox.launcher_1.8.0.v20260804-1928.jar \
  -configuration /tmp/ak-p2-configuration \
  -data /tmp/ak-p2-workspace \
  -nosplash -consolelog \
  -application org.eclipse.ant.core.antRunner \
  -buildfile examples/p2-mirror/build.xml
```

The isolated configuration under `/tmp/ak-p2-configuration` copies the installed
Eclipse configuration and changes `eclipse.p2.data.area` to
`file:/tmp/ak-p2-data/`; recreate it after a reboot. The persistent truststore
used above is described under **HTTPS certificates** below. TLS certificate
verification stays on.

By default the output is `examples/p2-mirror/mirror/`. Override with
`-Ddestination=/absolute/path` and override the source with `-Dsource=URL`.
`eclipse.p2.mirrors=false` keeps artifact requests on Artifact Keeper instead
of using Eclipse download mirrors. Repository references and source-specific
repository properties are omitted from the destination.

Use **Add → Local** in Eclipse to select the completed `mirror` directory
for an offline installation. Uncheck **Contact all update sites during install
to find required software** to keep dependency lookup on this selected site.
The public repositories can be read without a token; the provisioning token
is not stored in these files. Trust the localhost Caddy certificate in the
Eclipse UI if prompted. `localhost` refers to this machine; clients on other
machines need a reachable hostname and catalogs generated for that hostname.

The source catalogs report unresolved dependencies for EPP package roots on
`org.eclipse.justj.epp.feature.group` (the separately distributed Java runtime)
and for `org.eclipse.wildwebdeveloper.tests` on `org.eclipse.ui.tests.harness`.
Mirroring the advertised files does not make those external dependencies
available. Ordinary feature installation into an existing Eclipse IDE and
provisioning a complete EPP distribution have different dependency requirements.

## Completed mirror (20 October 2026)

Eclipse Ant reported `BUILD SUCCESSFUL`. The flat mirror contains 6,081
installable units and 4,987 unique artifacts: 4,334 plugins, 552 features, and
101 binaries (2,133,087,904 bytes). All source IU coordinates and artifact
files are present. Checksums are verified against the artifact descriptors
selected by Eclipse; 12 source artifacts do not advertise checksums.
See `mirror/verification.json` and `mirror.log` for the verification and run log.

Three source-bundle coordinates have different bytes in the Eclipse release
and Wild Web Developer repositories: `org.apache.commons.lang3.source` 3.20.0,
`org.snakeyaml.engine.source` 2.10.0, and
`org.apache.commons.commons-compress.source` 1.28.0. Eclipse selected the release
repository copies for the flat mirror, consistent with member precedence;
those files match the resulting artifact metadata. Both upstream variants
remain cached separately in their Artifact Keeper remote repositories.

## What was changed on the running server

All five repositories use `format: "p2"` and `is_public: true`. The four
upstream-backed repositories use `repo_type: "remote"` and the `upstream_url`
shown above. `p2` uses `repo_type: "virtual"` with members
`p2-eclipse-2026-09`, `p2-bash-editor`, and `p2-wildwebdeveloper`, in that order.
The EPP helper is referenced by the Eclipse release composite, rather than
attached directly to the virtual repository.

Metadata was manually uploaded to **both the virtual repository and one remote**:

| Repository | Manually published files | Purpose |
| --- | --- | --- |
| `p2` | `p2.index`, `compositeContent.xml`, `compositeContent.jar`, `compositeArtifacts.xml`, `compositeArtifacts.jar` | Advertise all three member update sites |
| `p2-eclipse-2026-09` | The same five filenames | Replace the upstream composite's escaping relative EPP child with an Artifact Keeper URL |
| `p2-bash-editor` | None | Proxy upstream metadata unchanged |
| `p2-wildwebdeveloper` | None | Proxy upstream metadata unchanged |
| `p2-eclipse-epp-2026-09` | None | Proxy upstream metadata unchanged |

These manually uploaded files are hosted artifacts, distinct from the upstream
metadata and JARs fetched into the remote caches. No feature/plugin JARs were
uploaded to the virtual repository. The local mirror was not uploaded either.

The shared download handler serves a repository's directly stored files first.
On a miss, a remote repository proxies its upstream; a virtual repository tries
its members by priority. Consequently, the explicit virtual composite wins over
the members' similarly named files. Eclipse then follows the composite children
to the individual remote repositories. This uses the generic artifact API's
current behavior; it is not automatic native p2 virtual aggregation.

Both XML and compressed JAR catalogs were published because Eclipse can probe
the JAR first. Publishing only an XML override can leave the upstream JAR visible
through the fallback and cause Eclipse to load the wrong child URLs.

The deployment change was in `artifact-keeper/docker/Caddyfile`:

```caddyfile
reverse_proxy /general/* backend:8080
```

The running `artifact-keeper-caddy` container bind-mounts that file at
`/etc/caddy/Caddyfile`. Its configuration was validated and reloaded with:

```bash
docker exec artifact-keeper-caddy caddy validate --config /etc/caddy/Caddyfile
docker exec artifact-keeper-caddy caddy reload --config /etc/caddy/Caddyfile
```

The top-level `docker/Caddyfile` was not modified in this setup. If deploying
with a different Caddyfile, add the route to the file that deployment actually
mounts. No Rust backend code or dedicated `/p2/` handler was added. `/general/`
is an existing shared file-download handler, and the final `p2` URL segment is
the repository key.

## Generate the composite metadata

[`generate-composite.py`](generate-composite.py) provides the generation logic
used for this setup as a reusable, standard-library-only Python script. The
original setup used inline Python; this script reproduces the same catalog
structure, with a fresh timestamp and serialization. It creates files locally
and does not call the API, create repositories, inspect membership, or publish.

From the repository root, generate the virtual catalog:

```bash
python3 examples/p2-mirror/generate-composite.py \
  --output /tmp/ak-p2-catalogs/p2 \
  --name p2 \
  --child https://localhost/general/p2-eclipse-2026-09/ \
  --child https://localhost/general/p2-bash-editor/ \
  --child https://localhost/general/p2-wildwebdeveloper/
```

Generate the Eclipse remote's replacement composite:

```bash
python3 examples/p2-mirror/generate-composite.py \
  --output /tmp/ak-p2-catalogs/p2-eclipse-2026-09 \
  --name p2-eclipse-2026-09 \
  --child https://localhost/general/p2-eclipse-epp-2026-09/ \
  --child 202609091000/
```

The relative snapshot child is resolved below the Eclipse remote's own URL.
Its upstream remains `https://download.eclipse.org/releases/2026-09`; the
replacement catalog pins the snapshot to `202609091000`. This does not track
future changes to that upstream composite automatically.

Each command emits exactly five files. The content catalog's type is
`org.eclipse.equinox.internal.p2.metadata.repository.CompositeMetadataRepository`;
the artifact catalog's type is
`org.eclipse.equinox.internal.p2.artifact.repository.CompositeArtifactRepository`.
Both have a `<children>` list of update-site locations. Each JAR contains its
corresponding XML file. `p2.index` selects the composite repository factories.
This generates links to existing metadata; it does not generate feature/plugin
installable-unit descriptions or merge upstream `content.jar` files.

To publish a generated catalog to an existing repository, use the generic API
with a token supplied through the environment. For example, for a fresh `p2`:

```bash
# Set API_TOKEN in your shell without storing it in this repository.
for file in /tmp/ak-p2-catalogs/p2/*; do
  curl --fail-with-body \
    --cacert .local/p2-tls/caddy-root.crt \
    -H "Authorization: Bearer ${API_TOKEN:?Set API_TOKEN first}" \
    -H 'Content-Type: application/octet-stream' \
    --upload-file "$file" \
    "https://localhost/api/v1/repositories/p2/artifacts/$(basename "$file")" || break
done
```

Use the analogous repository key and generated directory for the Eclipse
remote. Existing files may return HTTP 409 because this deployment treats
these hosted artifacts as immutable. During the setup, only the catalog files
created for this task were deleted and reuploaded to change their URLs. Review
and back up existing metadata before replacing it. Multi-file publication is
not atomic; clients can observe mixed catalogs during replacement.

When membership, hostname, or the release snapshot changes, regenerate and
republish the affected catalogs. Changing virtual membership alone does not
update these static files. Use the hostname reachable by Eclipse for every
absolute child URL; `localhost` only works on the server's own machine.

## HTTPS certificates

p2 does not require a special certificate format or a client certificate here.
It uses HTTPS through Eclipse's Java runtime, which must trust Artifact Keeper's
server certificate chain. There are two separate TLS connections:

1. **Eclipse → Artifact Keeper:** this localhost deployment uses Caddy's private
   local CA, which the IDE's Java truststore did not trust initially. That caused
   `PKIX path building failed` before Eclipse could read p2 metadata.
2. **Artifact Keeper → upstream update sites:** the backend uses its own TLS
   trust configuration. The public Eclipse and GitHub Pages sites downloaded
   successfully; no upstream-specific certificates were added to the backend.

For the IDE, Caddy's public root CA was exported, its chain was verified, and a
copy of Java 25's default truststore was extended with that CA. The persistent
files are `.local/p2-tls/caddy-root.crt` and `.local/p2-tls/cacerts` in this checkout.
No Caddy private key was exported, and the system Java truststore was not edited.
The setup commands, shown for reproduction, are:

```bash
mkdir -p .local/p2-tls
docker exec artifact-keeper-caddy \
  cat /data/caddy/pki/authorities/local/root.crt \
  > .local/p2-tls/caddy-root.crt

# Only copy the default store when creating a new dedicated store.
cp /usr/lib/jvm/java-25-openjdk/lib/security/cacerts .local/p2-tls/cacerts
chmod u+w .local/p2-tls/cacerts
/usr/lib/jvm/java-25-openjdk/bin/keytool -importcert \
  -alias artifact-keeper-caddy-root \
  -file .local/p2-tls/caddy-root.crt \
  -keystore .local/p2-tls/cacerts -storepass changeit
```

Verify the root certificate's fingerprint through your trusted server before
accepting it. Trusting this CA allows certificates it signs to be accepted by
that Eclipse process; it is not restricted to a single repository URL.

These options were appended after `-vmargs` in
`/home/seb/eclipse/committers-2026-09/eclipse/eclipse.ini`:

```text
-Djavax.net.ssl.trustStore=/home/seb/Developer/artifact-keeper/.local/p2-tls/cacerts
-Djavax.net.ssl.trustStorePassword=changeit
```

The original configuration was backed up as
`eclipse.ini.before-artifact-keeper-tls` beside `eclipse.ini`. Fully restart
Eclipse after changing these options. The Ant runner needs the same JVM
options before `-jar`; `build.xml` alone does not configure Java's TLS trust.
A Java HTTPS request using this truststore successfully fetched Wild Web
Developer's `content.jar` with HTTP 200.

The initial Ant run used a temporary `/tmp` truststore; the later IDE fix
created the persistent one above. Do not rely on the temporary file after a
reboot. If Caddy's CA changes, update the dedicated store. If Java's default
trusted roots change, rebuild the copy and reimport the local CA. For a server
certificate already trusted by Eclipse's Java runtime, no additional
truststore is necessary. Keep certificate validation enabled.
