#!/usr/bin/env python3

"""
Mirror registry dependencies into Artifact Keeper using Python 3.11+ and npm.
No third-party Python modules or Makefile are required.

Configuration:
    export ARTIFACT_KEEPER_TOKEN=...
    export AK_API=https://localhost/npm/npm

Choose exactly one input:
    python3 seed-npm.py --package-lock /path/to/package-lock.json
    python3 seed-npm.py --package-json /path/to/package.json
    python3 seed-npm.py --dependency '@scope/package@1.2.3'

Prefer --package-lock for reproducible seeding of exact transitive versions.
Lockfile versions 1, 2 and 3 and npm-shrinkwrap.json are supported.
--package-json resolves afresh, without using adjacent lockfiles or .npmrc.
It mirrors dependencies, not the root project itself. Generate workspace
lockfiles inside their original project before using --package-lock.
--dependency accepts registry names, versions, ranges, tags and npm aliases.

Use --ca-bundle with a PEM bundle containing public roots and your local CA.
NPM selects the npm executable. All other options are CLI arguments.
Each run uses a temporary workspace/cache and removes its authentication file.
Lifecycle scripts are disabled while resolving, packing and publishing.
Tarballs are checked against lockfile hashes, when present, and package identity.
Existing target name/version pairs are skipped, without comparing their bytes.

Scope:
    All registry entries recorded in the lockfile are mirrored, including dev,
    peer, optional and platform-specific entries. Bundled dependencies travel
    inside their parent's tarball; workspace links remain local links.
    Missing platform entries cannot be recovered from an incomplete lockfile.
    Git, local file/tarball and arbitrary URL dependencies are not converted
    into registry releases. Private upstream authentication, Yarn/pnpm locks,
    install-script binary downloads and native build tools need separate setup.

Publishing uses --publish-tag seeded by default, so historical releases do not
overwrite an existing latest tag. Upstream dist-tags, deprecation notices and
provenance are not mirrored. Use exact versions/ranges for reproducible installs.

Install using npm install or npm ci with --registry https://localhost/npm/npm/,
plus client authentication and CA trust. Locks with custom resolved hosts and
direct URL dependencies may still access those hosts; --registry alone does
not rewrite every source. Commit both package.json and package-lock.json.
Publish your own prepared package separately with npm publish <folder-or-tgz>.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from pathlib import Path, PurePosixPath


DEFAULT_PUBLIC_REGISTRY = "https://registry.npmjs.org/"
DEFAULT_WORK_DIR = "/tmp/artifact-keeper-npm-seed"
DEFAULT_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"

HTTP_TIMEOUT = 30
NPM_TIMEOUT = 300


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Download npm dependencies from the Internet and "
            "publish missing versions to Artifact Keeper."
        )
    )

    inputs = parser.add_mutually_exclusive_group(required=True)

    inputs.add_argument(
        "--package-lock",
        type=Path,
        help="Existing package-lock.json",
    )

    inputs.add_argument(
        "--package-json",
        type=Path,
        help="package.json whose dependencies should be mirrored",
    )

    inputs.add_argument(
        "--dependency",
        help=(
            "Single npm dependency, optionally with a version. "
            "Examples: lodash, lodash@4.17.21, "
            "@babel/core@7.28.4"
        ),
    )

    parser.add_argument(
        "--artifact-keeper",
        default=os.environ.get("AK_API"),
        help="Artifact Keeper npm registry URL (default: AK_API)",
    )

    parser.add_argument(
        "--public-registry",
        default=DEFAULT_PUBLIC_REGISTRY,
        help="Internet npm registry",
    )

    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path(DEFAULT_WORK_DIR),
        help="Parent of temporary workspaces; existing files are preserved",
    )

    parser.add_argument(
        "--ca-bundle",
        type=Path,
        default=Path(DEFAULT_CA_BUNDLE),
    )

    parser.add_argument(
        "--http-timeout",
        type=int,
        default=HTTP_TIMEOUT,
    )

    parser.add_argument(
        "--npm-timeout",
        type=int,
        default=NPM_TIMEOUT,
    )

    parser.add_argument(
        "--publish-tag",
        default="seeded",
        help="Tag for mirrored versions (default: seeded; upstream tags are not copied)",
    )

    args = parser.parse_args()

    if not args.artifact_keeper:
        parser.error("--artifact-keeper or AK_API is required")

    for url in (args.artifact_keeper, args.public_registry):
        validate_registry_url(url)

    if args.http_timeout <= 0 or args.npm_timeout <= 0:
        parser.error("Timeouts must be positive")

    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*", args.publish_tag):
        parser.error("Publish tag must start with a letter and contain letters, digits, _ or -")

    if args.dependency is not None:
        validate_dependency_spec(args.dependency)

    for name in ("package_lock", "package_json", "work_dir", "ca_bundle"):
        path = getattr(args, name)
        if path is not None:
            setattr(args, name, path.expanduser().resolve())

    return args


def validate_dependency_spec(spec: str):
    name = r"(?:@[a-zA-Z0-9_.-]+/)?[a-zA-Z0-9_][a-zA-Z0-9_.-]*"
    selector = r"[a-zA-Z0-9.*~^<>=|+ -]+"
    if not re.fullmatch(rf"{name}(?:@(?:{selector}|npm:{name}(?:@{selector})?))?", spec):
        raise ValueError("Use a registry package name with a version, range, tag or npm alias; Git/file/URL sources are not supported")


def validate_registry_url(url: str):
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in url)
    ):
        raise ValueError("Registry URLs must be HTTP(S) URLs without credentials, query or fragment")


def normalize_registry(url: str) -> str:
    return url.rstrip("/") + "/"


def npm_environment(ca_bundle: Path):
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.lower().startswith("npm_config_")
        and name not in ("ARTIFACT_KEEPER_TOKEN", "NPM_TOKEN", "NODE_AUTH_TOKEN", "NODE_ENV")
    }

    env["NPM_CONFIG_UPDATE_NOTIFIER"] = "false"
    env["NPM_CONFIG_STRICT_SSL"] = "true"
    env["NODE_EXTRA_CA_CERTS"] = str(ca_bundle)
    env["SSL_CERT_FILE"] = str(ca_bundle)
    env["NPM_CONFIG_CAFILE"] = str(ca_bundle)
    env["NPM_CONFIG_PROVENANCE"] = "false"

    return env


def run_command(
    command,
    *,
    cwd=None,
    env=None,
    timeout=NPM_TIMEOUT,
    capture_output=True,
):
    if not capture_output:
        sys.stdout.flush()
        sys.stderr.flush()

    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            timeout=timeout,
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
        )

    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Command timed out after {timeout}s:\n"
            f"  {' '.join(map(str, command))}"
        ) from exc

    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr or ""
        stdout = exc.stdout or ""

        raise RuntimeError(
            f"Command failed:\n"
            f"  {' '.join(map(str, command))}\n"
            f"{stdout}"
            f"{stderr}"
        ) from exc


def generate_lock_from_package_json(
    npm: str,
    package_json: Path,
    resolution_dir: Path,
    public_registry: str,
    cache_dir: Path,
    env,
    timeout: int,
) -> Path:
    print("==> Resolving package.json")
    print(f"    {package_json}")
    print()

    resolution_dir.mkdir(parents=True, exist_ok=True)
    destination = resolution_dir / "package.json"
    manifest = json.loads(package_json.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise RuntimeError("package.json must contain a JSON object")
    if manifest.get("workspaces"):
        raise RuntimeError("For workspaces, generate a lockfile in the project and use --package-lock")

    for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        dependencies = manifest.get(section, {})
        if not isinstance(dependencies, dict):
            raise RuntimeError(f"{section} must contain an object")
        for name, spec in dependencies.items():
            if not isinstance(spec, str):
                raise RuntimeError(f"Invalid dependency specifier for {name}")
            validate_dependency_spec(f"{name}@{spec}")

    shutil.copy2(package_json, destination)

    command = [
        npm,
        "install",
        "--package-lock-only",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        "--include=dev",
        "--include=optional",
        "--include=peer",
        "--registry",
        public_registry,
        "--cache",
        str(cache_dir),
        "--prefer-online",
        "--userconfig",
        os.devnull,
    ]

    run_command(
        command,
        cwd=resolution_dir,
        env=env,
        timeout=timeout,
    )

    lock_file = resolution_dir / "package-lock.json"
    if not lock_file.is_file():
        raise RuntimeError("npm did not generate package-lock.json")

    return lock_file


def generate_lock_from_dependency(
    npm: str,
    dependency: str,
    resolution_dir: Path,
    public_registry: str,
    cache_dir: Path,
    env,
    timeout: int,
) -> Path:
    print("==> Resolving dependency")
    print(f"    {dependency}")
    print()

    resolution_dir.mkdir(parents=True, exist_ok=True)

    package_json = {
        "name": "artifact-keeper-npm-seed",
        "version": "1.0.0",
        "private": True,
    }

    (resolution_dir / "package.json").write_text(
        json.dumps(package_json, indent=2) + "\n",
        encoding="utf-8",
    )

    command = [
        npm,
        "install",
        "--package-lock-only",
        "--ignore-scripts",
        "--save-exact",
        "--no-audit",
        "--no-fund",
        "--include=dev",
        "--include=optional",
        "--include=peer",
        "--registry",
        public_registry,
        "--cache",
        str(cache_dir),
        "--prefer-online",
        "--userconfig",
        os.devnull,
        "--",
        dependency,
    ]

    run_command(
        command,
        cwd=resolution_dir,
        env=env,
        timeout=timeout,
    )

    lock_file = resolution_dir / "package-lock.json"
    if not lock_file.is_file():
        raise RuntimeError(
            "npm did not generate package-lock.json"
        )

    return lock_file


def resolve_package_lock(
    args,
    npm: str,
    resolution_dir: Path,
    cache_dir: Path,
    env,
) -> Path:
    if args.package_lock:
        if not args.package_lock.is_file():
            raise RuntimeError(
                f"package-lock.json does not exist: "
                f"{args.package_lock}"
            )

        print("==> Using package-lock.json")
        print(f"    {args.package_lock}")
        print()

        return args.package_lock

    if args.package_json:
        if not args.package_json.is_file():
            raise RuntimeError(
                f"package.json does not exist: "
                f"{args.package_json}"
            )

        return generate_lock_from_package_json(
            npm,
            args.package_json,
            resolution_dir,
            args.public_registry,
            cache_dir,
            env,
            args.npm_timeout,
        )

    return generate_lock_from_dependency(
        npm,
        args.dependency,
        resolution_dir,
        args.public_registry,
        cache_dir,
        env,
        args.npm_timeout,
    )


def add_dependency(dependencies, name, metadata, public_registry):
    if not isinstance(metadata, dict):
        raise RuntimeError(f"Invalid lockfile entry for {name}")

    if metadata.get("inBundle") or metadata.get("bundled"):
        # These bytes are already included in the parent's tarball.
        return

    version = metadata.get("version")
    name = metadata.get("name") or name

    if isinstance(version, str) and version.startswith("npm:"):
        name, _, version = version[4:].rpartition("@")

    if not isinstance(name, str) or not re.fullmatch(r"(?:@[a-zA-Z0-9_.-]+/)?[a-zA-Z0-9_][a-zA-Z0-9_.-]*", name):
        raise RuntimeError(f"Invalid package name in lockfile: {name}")

    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", version):
        raise RuntimeError(f"Unsupported or missing registry version for {name}: {version}")

    resolved = metadata.get("resolved")
    if resolved:
        # Refuse to silently substitute a registry release for a Git/file/URL package.
        validate_registry_url(resolved)
        registries = (normalize_registry(public_registry), DEFAULT_PUBLIC_REGISTRY)
        if not any(resolved.startswith(registry) for registry in registries):
            raise RuntimeError(f"Unsupported source for {name}@{version}; set --public-registry to its source registry")

    key = (name, version)
    dependency = dependencies.setdefault(key, {
        "name": name,
        "version": version,
        "resolved": resolved,
        "integrities": set(),
    })
    integrity = metadata.get("integrity")
    if integrity:
        if not isinstance(integrity, str):
            raise RuntimeError(f"Invalid integrity for {name}@{version}")
        dependency["integrities"].add(integrity)


def load_dependencies_v2_v3(lock: dict, public_registry: str):
    dependencies = {}
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        raise RuntimeError("Lockfile version 2/3 must contain a packages object")

    marker = "node_modules/"

    for package_path, metadata in packages.items():
        if not package_path:
            continue
        if not isinstance(metadata, dict):
            raise RuntimeError(f"Invalid lockfile entry: {package_path}")
        if metadata.get("link"):
            if metadata.get("resolved") not in packages:
                raise RuntimeError(f"Local link target is missing from lockfile: {package_path}")
            print(f"LOCAL: {package_path} remains a workspace/link; only registry dependencies are mirrored")
            continue
        if marker not in package_path:
            continue

        name = package_path.rsplit(marker, 1)[1]
        add_dependency(dependencies, name, metadata, public_registry)

    return dependencies


def load_dependencies_v1(lock: dict, public_registry: str):
    dependencies = {}

    def visit(items):
        if not isinstance(items, dict):
            raise RuntimeError("Lockfile dependencies must be an object")

        for name, metadata in items.items():
            add_dependency(dependencies, name, metadata, public_registry)
            visit(metadata.get("dependencies", {}))

    visit(lock.get("dependencies", {}))
    return dependencies


def load_dependencies(package_lock: Path, public_registry: str):
    try:
        lock = json.loads(package_lock.read_text(encoding="utf-8"))

    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {package_lock}: {exc}") from exc

    if not isinstance(lock, dict):
        raise RuntimeError("Lockfile must contain a JSON object")

    version = lock.get("lockfileVersion")
    if version in (2, 3):
        dependencies = load_dependencies_v2_v3(lock, public_registry)
    elif version == 1:
        dependencies = load_dependencies_v1(lock, public_registry)
    else:
        raise RuntimeError(f"Unsupported lockfileVersion: {version}; expected 1, 2 or 3")

    return sorted(
        dependencies.values(),
        key=lambda item: (item["name"].lower(), item["version"]),
    )


def verify_tarball(tarball: Path, dependency):
    for integrity in dependency["integrities"]:
        digests = {}
        for part in integrity.split():
            algorithm, separator, digest = part.partition("-")
            if separator and algorithm in ("sha512", "sha384", "sha256", "sha1"):
                digests.setdefault(algorithm, []).append(digest.split("?", 1)[0])
        if not digests:
            raise RuntimeError(f"Unsupported lockfile integrity for {tarball.name}")
        algorithm = next(name for name in ("sha512", "sha384", "sha256", "sha1") if name in digests)
        with tarball.open("rb") as stream:
            actual = base64.b64encode(hashlib.file_digest(stream, algorithm).digest()).decode()
        if not any(hmac.compare_digest(actual, expected) for expected in digests[algorithm]):
            raise RuntimeError(f"Lockfile integrity mismatch for {tarball.name}")

    with tarfile.open(tarball, "r:gz") as archive:
        manifests = [
            member
            for member in archive
            if len(PurePosixPath(member.name).parts) == 2
            and PurePosixPath(member.name).name == "package.json"
        ]
        if len(manifests) != 1:
            raise RuntimeError(f"Expected one top-level package manifest in {tarball.name}")
        member = manifests[0]
        if not member.isfile() or member.size > 10 * 1024 * 1024:
            raise RuntimeError(f"Invalid package manifest in {tarball.name}")
        with archive.extractfile(member) as stream:
            manifest = json.load(stream)

    if manifest.get("name") != dependency["name"] or manifest.get("version") != dependency["version"]:
        raise RuntimeError(f"Tarball identity does not match lockfile: {tarball.name}")


def create_ssl_context(ca_bundle: Path):
    return ssl.create_default_context(cafile=str(ca_bundle))


def package_exists(
    registry: str,
    token: str,
    name: str,
    version: str,
    ssl_context,
    timeout: int,
) -> bool:
    encoded_name = urllib.parse.quote(
        name,
        safe="@/",
    )

    encoded_version = urllib.parse.quote(
        version,
        safe="",
    )

    url = (
        registry
        + encoded_name
        + "/"
        + encoded_version
    )

    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            context=ssl_context,
            timeout=timeout,
        ):
            return True

    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False

        raise RuntimeError(
            f"Artifact Keeper returned HTTP "
            f"{exc.code} while checking "
            f"{name}@{version}"
        ) from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Cannot connect to Artifact Keeper "
            f"while checking {name}@{version}: "
            f"{exc}"
        ) from exc


def create_npmrc(
    npmrc: Path,
    registry: str,
    token: str,
):
    parsed = urllib.parse.urlparse(registry)

    auth_path = parsed.path
    if not auth_path.endswith("/"):
        auth_path += "/"

    content = (
        f"registry={registry}\n"
        f"//{parsed.netloc}{auth_path}:"
        f"_authToken={token}\n"
    )

    with os.fdopen(os.open(npmrc, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        stream.write(content)


def npm_pack(
    npm: str,
    spec: str,
    public_registry: str,
    pack_dir: Path,
    cache_dir: Path,
    env,
    timeout: int,
) -> Path:
    command = [
        npm,
        "pack",
        spec,
        "--ignore-scripts",
        "--registry",
        public_registry,
        "--pack-destination",
        str(pack_dir),
        "--cache",
        str(cache_dir),
        "--prefer-online",
        "--userconfig",
        os.devnull,
        "--json",
    ]

    result = run_command(
        command,
        cwd=pack_dir,
        env=env,
        timeout=timeout,
    )

    try:
        data = json.loads(result.stdout)
        filename = data[0]["filename"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            f"Could not determine tarball "
            f"name for {spec}.\n"
            f"npm output:\n"
            f"{result.stdout}"
        ) from exc

    if not isinstance(filename, str) or Path(filename).name != filename:
        raise RuntimeError("npm pack returned an invalid filename")

    tarball = pack_dir / filename
    if not tarball.is_file():
        raise RuntimeError(
            f"npm pack reported "
            f"'{filename}', but the file "
            f"does not exist"
        )

    return tarball


def npm_publish(
    npm: str,
    tarball: Path,
    registry: str,
    npmrc: Path,
    cache_dir: Path,
    env,
    timeout: int,
    tag: str,
    name: str,
):
    command = [
        npm,
        "publish",
        str(tarball),
        "--registry",
        registry,
        "--userconfig",
        str(npmrc),
        "--cache",
        str(cache_dir),
        "--provenance=false",
        "--ignore-scripts",
        "--tag",
        tag,
    ]

    if name.startswith("@"):
        command.append(f"--{name.split('/')[0]}:registry={registry}")

    run_command(
        command,
        env=env,
        timeout=timeout,
        cwd=tarball.parent,
        capture_output=False,
    )


def main():
    try:
        args = parse_args()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    token = os.environ.get("ARTIFACT_KEEPER_TOKEN")
    if not token:
        print(
            "ERROR: ARTIFACT_KEEPER_TOKEN "
            "is not set",
            file=sys.stderr,
        )
        return 1

    if not args.ca_bundle.is_file():
        print(
            f"ERROR: CA bundle does not exist: "
            f"{args.ca_bundle}",
            file=sys.stderr,
        )
        return 1

    npm_command = os.environ.get(
        "NPM",
        "npm",
    )

    npm = shutil.which(npm_command)

    if not npm:
        print(
            f"ERROR: npm executable not found: "
            f"{npm_command}",
            file=sys.stderr,
        )
        return 1

    try:
        args.work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="run-", dir=args.work_dir) as directory:
            return seed(args, npm, token, Path(directory))

    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def seed(args, npm: str, token: str, work_dir: Path):
    args.artifact_keeper = normalize_registry(args.artifact_keeper)
    args.public_registry = normalize_registry(args.public_registry)

    pack_dir = work_dir / "packages"
    cache_dir = work_dir / "npm-cache"
    resolution_dir = work_dir / "resolution"
    npmrc = work_dir / "artifact-keeper.npmrc"

    work_dir.mkdir(parents=True, exist_ok=True)
    # Stop npm from discovering a parent project's package.json and .npmrc.
    (work_dir / "package.json").write_text(
        json.dumps({"name": "artifact-keeper-npm-seed", "private": True}) + "\n",
        encoding="utf-8",
    )
    pack_dir.mkdir(parents=True, exist_ok=True)

    cache_dir.mkdir(parents=True, exist_ok=True)

    env = npm_environment(args.ca_bundle)
    global_config = work_dir / "global.npmrc"
    global_config.write_text("", encoding="utf-8")
    env["NPM_CONFIG_GLOBALCONFIG"] = str(global_config)

    ssl_context = create_ssl_context(args.ca_bundle)

    try:
        package_lock = resolve_package_lock(
            args,
            npm,
            resolution_dir,
            cache_dir,
            env,
        )

        dependencies = load_dependencies(package_lock, args.public_registry)

    except Exception as exc:
        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )
        return 1

    print(
        f"Found {len(dependencies)} "
        f"unique dependency versions."
    )
    print()

    create_npmrc(npmrc, args.artifact_keeper, token)

    total = len(dependencies)
    uploaded = 0
    skipped = 0
    failed = 0

    try:
        for dependency in dependencies:
            name = dependency["name"]
            version = dependency["version"]
            spec = f"{name}@{version}"

            print("-" * 60)
            print(f"Dependency: {spec}")

            try:
                exists = package_exists(
                    args.artifact_keeper,
                    token,
                    name,
                    version,
                    ssl_context,
                    args.http_timeout,
                )

            except Exception as exc:
                print(f"ERROR: {exc}", file=sys.stderr)

                # Do not interpret network/auth/TLS errors as "package missing".
                return 1

            if exists:
                print(f"SKIP: {spec} already exists in Artifact Keeper.")
                skipped += 1
                print()
                continue

            print(f"MISSING: {spec}")
            print(f"Downloading from: {args.public_registry}")

            try:
                tarball = npm_pack(
                    npm,
                    dependency["resolved"] or spec,
                    args.public_registry,
                    pack_dir,
                    cache_dir,
                    env,
                    args.npm_timeout,
                )

            except Exception as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                failed += 1
                print()
                continue

            print(f"Downloaded: {tarball}")
            print("Uploading to Artifact Keeper...")

            try:
                verify_tarball(tarball, dependency)

                npm_publish(
                    npm,
                    tarball,
                    args.artifact_keeper,
                    npmrc,
                    cache_dir,
                    env,
                    args.npm_timeout,
                    args.publish_tag,
                    name,
                )

                print(f"UPLOADED: {spec}")
                uploaded += 1

            except Exception as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                failed += 1

            finally:
                tarball.unlink(missing_ok=True)

            print()

    finally:
        # Contains the Artifact Keeper token.
        npmrc.unlink(missing_ok=True)

    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"Total:    {total}")
    print(f"Uploaded: {uploaded}")
    print(f"Skipped:  {skipped}")
    print(f"Failed:   {failed}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
