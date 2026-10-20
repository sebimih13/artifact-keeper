# Java project using Artifact Keeper

The existing Jackson demonstration is preserved, including its runtime dependency copy step. The extended Maven example consumes `org.apache.commons:commons-lang3:3.17.0` from `maven-external` and `example.ak:internal-greeter:1.0.0` from `maven-internal`, through the virtual `maven` repository. JUnit verifies that the application uses both libraries together. Use JDK 17+ and Maven 3.6.3+.

Run the following commands from the Artifact Keeper checkout root. The three repositories must already exist, with both local repositories included in the virtual one. `settings.xml` mirrors every Maven dependency/plugin repository to `https://localhost/maven/maven/`; use your VM's hostname when appropriate. No public repository URL is required in the consumer POM.

## 1. Acquire and seed public dependencies

On the Internet-connected staging machine:

```sh
export ARTIFACT_KEEPER_TOKEN='your-write-token'
export AK_CA_PEM='/path/to/artifact-keeper-ca.pem'

python3 seeders/maven/seed-maven.py \
  --pom-xml-file examples/java-proj/acquisition/pom.xml \
  --maven-goal verify \
  --artifact-keeper https://localhost/maven/maven-external \
  --ca-bundle "$AK_CA_PEM"
```

`acquisition/pom.xml` contains only public dependencies and the same pinned build plugins. Its small test makes Maven download the JUnit test provider as well. The internal library is deliberately absent from this acquisition POM. Update it when consumer public dependencies/plugins change.

## 2. Trust the local HTTPS server in Java

If the CA is already trusted by your JVM, skip this step. Otherwise create a dedicated truststore for local repository builds (choose an unused path):

```sh
keytool -importcert -noprompt -alias artifact-keeper \
  -file "$AK_CA_PEM" -keystore /tmp/ak-java-example-trust.p12 \
  -storetype PKCS12 -storepass changeit
export MAVEN_OPTS="${MAVEN_OPTS:-} -Djavax.net.ssl.trustStore=/tmp/ak-java-example-trust.p12 -Djavax.net.ssl.trustStorePassword=changeit"
```

This dedicated truststore trusts the imported CA only. Use it for the local-only builds below; unset these JVM options before further public acquisition, or use a truststore containing both public roots and your local CA. A truststore password protects certificate-store integrity; it is not your Artifact Keeper token.

## 3. Build the internal JAR, then upload it

Building is a separate, explicit Maven step. Even this build obtains its plugins through the virtual repository:

```sh
mvn -B -s examples/java-proj/settings.xml \
  -f examples/java-proj/internal-greeter/pom.xml package

python3 seeders/maven/seed-maven.py \
  --jar examples/java-proj/internal-greeter/target/internal-greeter-1.0.0.jar \
  --pom-file examples/java-proj/internal-greeter/pom.xml \
  --artifact-keeper https://localhost/maven/maven-internal \
  --ca-bundle "$AK_CA_PEM"
```

Repeat the upload to verify `Uploaded: 0`, `Skipped: 2`, `Failed: 0` and exit status 0. Omitting `--pom-file` also works for this Maven-built JAR because it contains a unique embedded POM. The seeder never builds in `--jar` mode.

## 4. Validate the consumer using a fresh cache

```sh
export AK_MAVEN_TEST_CACHE="$(mktemp -d /tmp/ak-java-cache.XXXXXX)"

mvn -B -s examples/java-proj/settings.xml \
  -Dmaven.repo.local="$AK_MAVEN_TEST_CACHE" \
  -f examples/java-proj/pom.xml clean verify

mvn -B -o -s examples/java-proj/settings.xml \
  -Dmaven.repo.local="$AK_MAVEN_TEST_CACHE" \
  -f examples/java-proj/pom.xml clean verify
```

The first build gets dependencies and plugins through Artifact Keeper only; the fresh cache prevents local artifacts from hiding missing repository files. The second build uses that cache without Maven repository network access. Both should report one passing `AppTest` and `BUILD SUCCESS`.

The main application can also run with those two dependency JARs:

```sh
java -cp "examples/java-proj/target/classes:$AK_MAVEN_TEST_CACHE/example/ak/internal-greeter/1.0.0/internal-greeter-1.0.0.jar:$AK_MAVEN_TEST_CACHE/org/apache/commons/commons-lang3/3.17.0/commons-lang3-3.17.0.jar" example.App
```

Expected: `Hello, Java from Artifact Keeper!`

See the [Maven seeder README](../../seeders/maven/README.md) for profiles, custom upstreams, coordinates, TLS, duplicate behavior and coverage limits.

The original Jackson entry point remains available after the consumer build:

```sh
java -cp 'examples/java-proj/target/classes:examples/java-proj/target/dependency/*' example.Main
```

`staging-settings.xml` and the older `upload-cache.py` remain available for the previous manual-cache workflow; the new seeder is the documented acquisition/upload path.
