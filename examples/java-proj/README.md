# Jackson through Artifact Keeper

This Java 17+ example serializes a Java record to JSON with Jackson and checks
that deserializing it returns the original value. It uses Jackson Databind
2.20.1, with Jackson Core 2.20.1 and Jackson Annotations 2.20 transitively.
Requires an already installed JDK and Maven 3.9.x (verified with Maven 3.9.11).

## Registry and normal build

Artifact Keeper's Maven endpoint is `/maven/{repository-key}/`. This project's
repository key is `maven`, so its URL is `http://localhost/maven/maven/`.
The repository is **local**, with `upstream_url: null`; missing artifacts will
fail instead of being fetched from Maven Central.

Run commands from this directory. `.mvn/maven.config` selects `settings.xml`,
whose `mirrorOf=*` sends dependency and plugin repository requests to Artifact
Keeper. On another VM, replace `localhost` in `settings.xml` with the Artifact
Keeper server hostname. `staging-settings.xml` is only for the explicitly
selected internet-connected preparation step below.

```bash
cd /home/seb/Developer/artifact-keeper/examples/java-proj
mvn -B -C -Dmaven.repo.local=.m2-artifact-keeper clean package
java -cp 'target/classes:target/dependency/*' example.Main
```

`-C` enables strict repository checksum checking. The executable code is also
packaged as `target/artifact-keeper-jackson-demo-1.0.0.jar`; runtime libraries
are copied into `target/dependency/`.

Expected output:

```text
Jackson version: 2.20.1
{"message":"Hello from Artifact Keeper","number":42}
JSON round-trip successful
```

## Prepare and upload dependencies

This one-time preparation requires an internet-connected staging machine.
For an air-gapped deployment, transfer `.m2-staging/` to a machine that can
reach Artifact Keeper and run the Python upload there. The server itself
does not need internet. Transfer the project files as well to build on VMs.

```bash
cd /home/seb/Developer/artifact-keeper/examples/java-proj

# Explicitly override the default settings for connected preparation only.
mvn -B -C -s staging-settings.xml \
  -Dmaven.repo.local=.m2-staging clean package > staging-build.log 2>&1

# Prompts for the admin password; no credentials are stored in the project.
python3 upload-cache.py .m2-staging > upload.log
```

The importer creates the `maven` repository if missing, refuses a remote
repository, and uploads only JAR and POM files using native Maven PUT requests.
It includes the parent POMs and build-plugin dependencies resolved by this build.
It verifies every uploaded file with SHA-256 and checks existing files without
overwriting them. Maven's cache bookkeeping files are not uploaded. Artifact
Keeper generates checksum responses for hosted files.

The verified import contained **312 files, 36,121,171 bytes**. These cover the
pinned `clean package` workflow; other Maven goals or new libraries may need
additional imports.

API requests are printed to `upload.log` without credentials:

```text
POST /api/v1/auth/login
GET  /api/v1/repositories/maven
POST /api/v1/repositories                    (only when missing)
GET  /maven/maven/<group-path>/<artifact>/<version>/<file>
PUT  /maven/maven/<group-path>/<artifact>/<version>/<file>  (only when missing)
GET  /maven/maven/<group-path>/<artifact>/<version>/<file>  (verification)
```

For example, Jackson Databind is served at:

```text
http://localhost/maven/maven/com/fasterxml/jackson/core/jackson-databind/2.20.1/jackson-databind-2.20.1.jar
```

## Verification commands used

The following build used a previously empty `.m2-artifact-keeper` directory:

```bash
mvn -B -C -Dmaven.repo.local=.m2-artifact-keeper clean package \
  > artifact-keeper-build.log 2>&1
java -cp 'target/classes:target/dependency/*' example.Main

# After populating the cache, rebuild without any repository access.
mvn -B -o -Dmaven.repo.local=.m2-artifact-keeper clean package \
  > offline-build.log 2>&1
java -cp 'target/classes:target/dependency/*' example.Main

curl -fsS http://localhost/api/v1/repositories/maven
```

The repository-only build log contains 312 successful downloads, all from
Artifact Keeper. To repeat with another empty cache without deleting anything:

```bash
MAVEN_PROOF_CACHE="$(mktemp -d /tmp/ak-maven-proof.XXXXXX)"
mvn -B -C -Dmaven.repo.local="$MAVEN_PROOF_CACHE" clean package
java -cp 'target/classes:target/dependency/*' example.Main
```

Maven's `-o` option prevents access to Artifact Keeper too. Use it only after
the local cache is populated; for offline VMs that can access the internal
Artifact Keeper server, the regular build is appropriate.
