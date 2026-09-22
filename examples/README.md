
# P2 Repository Update Site
```
egit/
├── 6.10.0/
│   ├── artifacts.jar
│   ├── content.jar
│   ├── features/
│   └── plugins/
├── 7.0.0/
│   ├── artifacts.jar
│   ├── content.jar
│   ├── features/
│   └── plugins/
├── compositeArtifacts.jar
└── compositeContent.jar
```

=> https://localhost/api/v1/repositories/egit/download/


# NPM Registry
```
npm install -g javascript-obfuscator@5.8.0 --registry=http://localhost/npm/npm/
```


# PIP Registry
```
pip install \
  --index-url http://localhost/pypi/pip/simple/ \
  --trusted-host localhost \
  artifact-keeper-greeter==2.0.0
```

# GO Registry
```
GOPROXY=http://localhost/go/go \
GONOPROXY=none \
GONOSUMDB='*' \
GOVCS='*:off' \
go get google.golang.org/genproto/googleapis/api@v0.0.0-20241007155032-5fefd90f89a9
```

```
cd /home/seb/Developer/artifact-keeper/examples/go-proj

gofmt -w main.go

export GOPROXY=http://localhost/go/go
export GONOPROXY=none
export GOSUMDB=off
export GOVCS='*:off'
export GOTOOLCHAIN=local
export GOWORK=off

export GOMODCACHE="$(mktemp -d /tmp/ak-go-proj-modules.XXXXXX)"
export GOCACHE="$(mktemp -d /tmp/ak-go-proj-build.XXXXXX)"

go mod download -x \
  google.golang.org/genproto/googleapis/api \
  google.golang.org/protobuf

go build -mod=readonly -o bin/go-proj .
go mod verify
./bin/go-proj
go version -m bin/go-proj
```


# Maven Registry
1. Add the repository to pom.xml (or settings.xml)

In pom.xml (project-specific):
```xml
<repositories>
  <repository>
    <id>artifactory-releases</id>
    <name>Internal Artifactory Releases</name>
    <url>https://artifactory.yourcompany.com/artifactory/libs-release-local</url>
  </repository>
  <repository>
    <id>artifactory-snapshots</id>
    <name>Internal Artifactory Snapshots</name>
    <url>https://artifactory.yourcompany.com/artifactory/libs-snapshot-local</url>
    <snapshots>
      <enabled>true</enabled>
    </snapshots>
  </repository>
</repositories>
```

Or globally in ~/.m2/settings.xml (applies to all projects, cleaner for shared/CI use) — use a <profile> with <repositories> and activate it by default:
```xml
<settings>
  <profiles>
    <profile>
      <id>artifactory</id>
      <repositories>
        <repository>
          <id>artifactory-releases</id>
          <url>https://artifactory.yourcompany.com/artifactory/libs-release-local</url>
        </repository>
        <repository>
          <id>artifactory-snapshots</id>
          <url>https://artifactory.yourcompany.com/artifactory/libs-snapshot-local</url>
          <snapshots>
            <enabled>true</enabled>
          </snapshots>
        </repository>
      </repositories>
    </profile>
  </profiles>
  <activeProfiles>
    <activeProfile>artifactory</activeProfile>
  </activeProfiles>
</settings>
```


2. Add authentication (if the repo requires it)

In ~/.m2/settings.xml, add a <server> entry whose id matches the repository id above:

```xml
<settings>
  <servers>
    <server>
      <id>artifactory-releases</id>
      <username>your-username</username>
      <password>your-api-key-or-encrypted-password</password>
    </server>
    <server>
      <id>artifactory-snapshots</id>
      <username>your-username</username>
      <password>your-api-key-or-encrypted-password</password>
    </server>
  </servers>
</settings>
```



