# Eclipse P2 seeder

`seed-p2.py` generates an update site from existing feature and plugin JARs and uploads it to Artifact Keeper. It uses Eclipse's official `FeaturesAndBundlesPublisher` to interpret OSGi manifests, dependencies and feature descriptors. Python handles packaging, checksums and uploads; it does not build your plugins.

## Input and generated site

Provide a directory like this (both subdirectories must exist):

```text
mirror/
├── features/
│   └── com.example.product_1.0.0.jar
└── plugins/
    └── com.example.plugin_1.0.0.jar
```

Feature JARs must contain `feature.xml`. Plugin JARs must be Eclipse/OSGi bundles with `META-INF/MANIFEST.MF`, including their bundle identity and version. An ordinary Java library JAR is not necessarily a valid bundle. This script accepts flat directories of JARs, not unpacked bundles or features. Either directory may be empty, but the site must contain at least one artifact.

The separate output directory contains:

```text
site/
├── features/             # Published feature JARs
├── plugins/              # Published bundle JARs
├── artifacts.jar         # ZIP containing artifacts.xml: artifact locations/checksums
├── artifacts.xml.xz      # XZ-compressed artifacts.xml
├── content.jar           # ZIP containing content.xml: installable units/dependencies
├── content.xml.xz        # XZ-compressed content.xml
└── p2.index              # Repository discovery and preferred metadata formats
```

JAR and XZ files contain equivalent XML. The index prefers XZ and allows the standard XML/JAR factory as fallback. `site.xml` is not needed for a modern P2 repository.

## Requirements

- Python 3.9+ with its standard library, including `lzma`; no pip packages.
- An Eclipse installation containing `org.eclipse.equinox.p2.publisher` and `org.eclipse.equinox.p2.publisher.eclipse`, plus its supported Java runtime. Supply the installation's actual Eclipse launcher, not a wrapper script. The seeder uses temporary configuration and workspace directories.
- For uploads: a **local Generic repository** in Artifact Keeper with **artifact versioning enabled** and a token allowed to read/write it. Versioning permits replacing the metadata at stable paths. The script keeps versioned plugin/feature JAR paths immutable.

This uses Artifact Keeper's raw artifact API to serve a static P2 site. Select **Generic**, not the native P2 format: the current P2 path parser does not accept every required compressed metadata/index filename.

## Generate locally

Run from the project root, using your existing mirror:

```bash
export ECLIPSE=/home/seb/eclipse/java-2026-06/eclipse/eclipse

python3 seeders/p2/seed-p2.py \
  --source-dir seeders/p2/mirror \
  --output-dir /tmp/artifact-keeper-p2-site \
  --generate-only
```

`--project-dir` is an alias for `--source-dir`. Source and output must not overlap. Keep the same output directory between runs to preserve unchanged metadata timestamps and avoid unnecessary uploads. Existing unrelated output files are not deleted; only files from the current generation are uploaded.

## Upload to Artifact Keeper

Create the repository described above, then set:

```bash
export ARTIFACT_KEEPER_TOKEN='<token with repository read/write access>'
export AK_API=https://localhost/api/v1/repositories/p2
export ECLIPSE=/home/seb/eclipse/java-2026-06/eclipse/eclipse

python3 seeders/p2/seed-p2.py \
  --source-dir seeders/p2/mirror \
  --output-dir /tmp/artifact-keeper-p2-site \
  --ca-bundle /path/to/local-ca-bundle.pem
```

`AK_API` is the repository API URL, **not** `/npm/npm`, `/p2/p2`, or the update-site URL. `--artifact-keeper` overrides it. `ECLIPSE` can instead be supplied as `--eclipse`; otherwise `eclipse` is looked up on PATH. No environment variables are needed for generation if `--eclipse` is provided.

For HTTPS, Python uses `/etc/ssl/certs/ca-certificates.crt` by default. Use `--ca-bundle` with a PEM trust bundle containing your local Caddy CA if that CA is not installed in the system bundle. Certificate verification stays enabled.

The Eclipse update-site URL is:

```text
https://localhost/api/v1/repositories/p2/download/
```

Optional `--site-path releases/1.0` places the entire site under that prefix and makes the update URL:

```text
https://localhost/api/v1/repositories/p2/download/releases/1.0/
```

The script prints per-file results and the same `Total`, `Uploaded`, `Skipped`, `Failed` summary as the other seeders. It downloads remote files to compare SHA-256, so checks require read access and consume bandwidth. Uploaded bytes are downloaded again for verification. On upload failure it withholds remaining files and exits nonzero; `Failed` includes those withheld files.

## Add or update a plugin

1. Add the new bundle JAR under `plugins/`.
2. For changed bundle contents, increment `Bundle-Version` and use a new versioned filename. Never replace the bytes of an already published version.
3. If a feature should install that plugin, update its `feature.xml` references and publish a new version of the feature JAR too.
4. Rerun the same command with the complete input directory.

The seeder uploads new JARs first, then changed artifact metadata, changed content metadata, and finally `p2.index`. Unchanged files are skipped, including `p2.index` when its discovery settings have not changed. It checks for conflicting existing JARs before uploading anything. The publisher must copy every input JAR unchanged; skipped invalid bundles or duplicate identities cause generation to fail before upload.

**Keep all versions you want advertised in the input.** Each run rebuilds metadata from the complete current input, not from the existing remote site. Removing an input removes it from generated metadata, but does not delete the remote JAR. The script does not resolve/download missing dependencies or validate that every feature can install into every Eclipse version. Update-site references embedded in feature descriptors are preserved by the publisher, so a mirrored feature can still reference its upstream update sites.

Serialize publishing runs. Replacing several metadata files is **not atomic**; clients can briefly observe different generations, and an interrupted update may leave mixed metadata. Rerun to finish a failed publication. For releases that require consistent snapshots, publish under a new immutable `--site-path` and manage a composite repository/release switch separately; this script does not generate composite metadata.

## Install in Eclipse

In **Help → Install New Software… → Add…**, enter the update-site URL. Features are the usual user-facing install choices; bundles alone are primarily dependency units. This seeder does not generate categories, so uncheck **Group items by category** to see uncategorized features. For curated categories, add a separate Eclipse CategoryPublisher step before publication; do not hand-edit generated XML.

Enable the additional update sites needed to resolve dependencies, or include those dependencies in your input if the site must work offline. Eclipse must trust your server certificate in its Java trust store; Python's `--ca-bundle` does not configure Eclipse. Private repositories also require client authentication through a method supported by your Eclipse installation/server, or anonymous read access. The seeder's `ARTIFACT_KEEPER_TOKEN` is not automatically passed to Eclipse clients. Do not put tokens into update-site URLs.

For all command options:

```bash
python3 seeders/p2/seed-p2.py --help
```

References: [Eclipse P2 publisher](https://help.eclipse.org/latest/topic/org.eclipse.platform.doc.isv/guide/p2_publisher.html), [P2 repository index](https://wiki.eclipse.org/Equinox/p2/p2_index).

## Tests

```bash
python3 -m unittest discover -s seeders/p2 -p 'test_*.py'
```

These tests use a temporary HTTP server to check upload order, repeat-run skips, new-plugin publication, conflicting JAR rejection and failure handling. They also verify XML preservation and timestamp reuse. They do not require Eclipse or a running Artifact Keeper server; a real publisher run is exercised separately using `--generate-only`.
