# Maven seeder

`seed-maven.py` downloads dependencies for a Maven POM or publishes an already built JAR. It uses Python's standard library; there is no Makefile and no Python package installation. Use Python 3.9+; POM mode also needs Maven 3.6.3+ and a JDK compatible with your project and plugins.

Your repositories are:

| Key | Type | Purpose |
| --- | --- | --- |
| `maven-external` | Local | Public dependencies and build plugins acquired on an Internet-connected machine |
| `maven-internal` | Local | Your own JARs and their POMs |
| `maven` | Virtual | Consumer endpoint combining both local repositories |

`--artifact-keeper` (or `AK_API`) is the **exact destination repository URL**, for example `https://localhost/maven/maven-external`. The seeder appends only Maven artifact paths; it does not choose a repository or append another repository key. Upload to a local repository, not the virtual one. Replace `localhost` with the Artifact Keeper hostname when running on a different machine.

## Credentials and TLS

From the repository root:

```sh
export ARTIFACT_KEEPER_TOKEN='your-write-token'
export AK_API='https://localhost/maven/maven-external'
```

The token needs read and upload access to the destination. Do not commit tokens. Upload authentication uses HTTP Basic with username `__token__` and the token as password. Existing artifacts are checked using authenticated GET requests.

Use `--ca-bundle /path/to/local-ca.pem` when the server uses a private CA. Python adds this CA to its system trust roots and verifies HTTPS. This option does **not** configure Maven's JVM. Maven needs the CA in Java's truststore when downloading from Artifact Keeper; see the example below. HTTP is accepted for explicitly configured local environments but does not protect credentials.

## Download dependencies from a POM

```sh
python3 seeders/maven/seed-maven.py \
  --pom-xml-file /path/to/project/pom.xml \
  --artifact-keeper https://localhost/maven/maven-external \
  --ca-bundle /path/to/local-ca.pem
```

Maven resolves the actual dependency graph, including transitive dependencies, parent POMs, imported BOMs, classifiers, project plugins and reporting plugins. Original JARs, POMs and other resolved artifacts retain their Maven directory layout. Local Maven tracking files, downloaded checksum files and repository metadata are not uploaded. Artifact Keeper serves checksums and generates repository metadata for stored artifacts.

The source defaults to `https://repo.maven.apache.org/maven2`, Maven Central's download endpoint. `mvnrepository.com` is a package search website, not a Maven repository endpoint. Use `--upstream-url` for a different download mirror, or `--maven-settings /path/to/staging-settings.xml` for multiple repositories, credentials and proxies. Explicit settings take precedence over `--upstream-url`.

Each run uses an empty temporary Maven cache. The default settings mirror all Maven repositories to Central and replace user/global settings for that process, so your VM-oriented `~/.m2/settings.xml` does not redirect acquisition back to Artifact Keeper. Project `.mvn` configuration and JVM options still apply. The upload token is removed from Maven's environment; upstream credentials must be configured separately.

The POM remains unchanged. Maven may create `target/` output in the project. Other files in `--work-dir` are preserved; only the run's temporary workspace is deleted. Nothing is uploaded if Maven resolution or an additional acquisition goal fails.

### Build coverage and limitations

The seeder runs the pinned `maven-dependency-plugin:3.11.0:go-offline` goal. Some plugins download additional artifacts only when executed, such as Surefire test providers. Add the build goals you need to exercise those paths before uploading:

```sh
python3 seeders/maven/seed-maven.py \
  --pom-xml-file /path/to/project/pom.xml \
  --profiles integration \
  --maven-goal verify \
  --artifact-keeper https://localhost/maven/maven-external
```

Additional goals execute the project's build code. Use build goals such as `test` or `verify`; do not use `install` or `deploy` to acquire external dependencies, since these publish/install project outputs. For a reactor, keep the original project structure and pass its root POM.

Coverage depends on active profiles, OS, JDK, classifiers and the goals executed. Run acquisition for each required environment. Inactive profiles, unused entries in `dependencyManagement`, `systemPath` JARs, downloaded native tools and arbitrary HTTP downloads are not automatically mirrored. Sources and Javadoc JARs are only included if actually resolved. This script supports fixed **release** artifacts; SNAPSHOT cache entries/publication are rejected before upload. Pin dependency and plugin versions; no upstream version-range or snapshot metadata is mirrored.

If a consumer also needs internal JARs, either provide upstream settings that can read both public and internal repositories, or use a separate acquisition POM containing the public dependencies and matching plugins. With mixed source settings, every resolved artifact is uploaded to the selected destination, including internal ones. The [Java example](../../examples/java-proj/README.md) uses a separate acquisition POM to keep external and internal artifacts in their respective repositories.

Always verify the real build with a fresh Maven cache through the virtual repository, then repeat with `mvn -o`. `go-offline` alone is not proof that every possible build path is covered.

## Upload an existing JAR

Supply its published POM to preserve dependency declarations:

```sh
python3 seeders/maven/seed-maven.py \
  --jar /path/to/internal-greeter-1.0.0.jar \
  --pom-file /path/to/pom.xml \
  --artifact-keeper https://localhost/maven/maven-internal \
  --ca-bundle /path/to/local-ca.pem
```

This never builds or downloads dependencies. Without `--pom-file`, the seeder uses the JAR's single `META-INF/maven/<groupId>/<artifactId>/pom.xml`, if present. For an ordinary JAR without Maven metadata, supply all coordinates:

```sh
python3 seeders/maven/seed-maven.py \
  --jar /path/to/my-library.jar \
  --group-id com.example --artifact-id my-library --version 1.0.0 \
  --artifact-keeper https://localhost/maven/maven-internal
```

Explicit coordinates without a POM generate a minimal POM with **no dependencies**, and print a warning. Prefer the real published POM. JAR filenames do not determine coordinates. Use `--classifier sources`, `javadoc`, or another classifier to publish an attached JAR with the same POM.

Coordinates must be concrete: unresolved `${revision}` and similar coordinate properties are rejected. Supply a resolved/flattened publication POM when necessary. An inherited group/version can be read from the POM's `<parent>`; that parent POM and referenced BOMs must also be available to consumers. The seeder preserves the original POM instead of silently removing inheritance or dependencies. Ambiguous embedded POMs require `--pom-file`.

## Existing versions and output

Files already present at the Maven coordinate path produce a warning and `SKIP`, with exit status **0** if there are no other failures. Differing existing bytes also warn and are never deliberately overwritten: publish a new version. Checks operate per file, so rerunning an interrupted publication can repair a missing POM or JAR. A POM and JAR upload are not an atomic transaction. Successful writes are read back and verified.

The output follows the PyPI seeder's format:

```text
============================================================
Summary
============================================================
Total:    2
Uploaded: 0
Skipped:  2
Failed:   0
```

Counts are repository files (POMs, JARs, etc.), not just libraries. Authentication, download and upload errors exit 1. Invalid command-line usage exits 2. HTTP and Maven timeouts can be changed with `--http-timeout` and `--command-timeout`.

## Consume both repositories without changing your POM

Put this in `~/.m2/settings.xml`, or pass it using `mvn -s /path/to/settings.xml`:

```xml
<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0">
  <mirrors>
    <mirror>
      <id>artifact-keeper</id>
      <mirrorOf>*</mirrorOf>
      <url>https://localhost/maven/maven/</url>
    </mirror>
  </mirrors>
  <servers>
    <server>
      <id>artifact-keeper</id>
      <username>__token__</username>
      <password>${env.ARTIFACT_KEEPER_TOKEN}</password>
    </server>
  </servers>
</settings>
```

This covers dependency and plugin repositories. Existing external dependency declarations in `pom.xml` stay unchanged; internal dependencies use normal Maven group/artifact/version declarations too. Maven looks in its local cache first, then uses Artifact Keeper. It does not fall back to Central if the mirror lacks a file. Keep both virtual members local to avoid server-side Internet access. Authentication can be omitted for public reads.

`AK_API` is a seeder variable; Maven itself does not interpret it. To parameterize Maven's mirror URL, explicitly use `<url>${env.AK_MAVEN_URL}</url>` and export `AK_MAVEN_URL=https://localhost/maven/maven/` on each client.

For a VM, `mvn verify` may contact your LAN Artifact Keeper even without Internet. `mvn -o verify` additionally prohibits Maven repository network requests, so it needs a previously populated client cache.

## Validation and references

```sh
python3 -m unittest discover -s seeders/maven -p 'test_*.py'
```

See [examples/java-proj](../../examples/java-proj/README.md) for a complete acquisition, internal upload and consumer build walkthrough.

- [Maven go-offline goal](https://maven.apache.org/components/plugins/maven-dependency-plugin/go-offline-mojo.html)
- [Maven mirror settings](https://maven.apache.org/guides/mini/guide-mirror-settings.html)
- [Maven repository layout](https://maven.apache.org/repositories/layout.html)
- [Maven third-party JAR deployment](https://maven.apache.org/guides/mini/guide-3rd-party-jars-remote.html)
