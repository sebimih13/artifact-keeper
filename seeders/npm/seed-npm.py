#!/usr/bin/env python3

import argparse
import json
import os
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from pathlib import Path


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
        required=True,
        help="Artifact Keeper npm registry URL",
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

    return parser.parse_args()


def normalize_registry(url: str) -> str:
    return url.rstrip("/") + "/"


def npm_environment(ca_bundle: Path):
    env = os.environ.copy()

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
    shutil.copy2(package_json, destination)

    command = [
        npm,
        "install",
        "--package-lock-only",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
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
        "--registry",
        public_registry,
        "--cache",
        str(cache_dir),
        "--prefer-online",
        "--userconfig",
        os.devnull,
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


def load_dependencies_v2_v3(lock: dict):
    dependencies = set()

    packages = lock.get("packages")
    if not isinstance(packages, dict):
        return dependencies

    marker = "node_modules/"

    for package_path, metadata in packages.items():
        # Root package
        if not package_path:
            continue

        if not isinstance(metadata, dict):
            continue

        # Workspace/symlink
        if metadata.get("link", False):
            continue

        version = metadata.get("version")
        if not version:
            continue

        if marker not in package_path:
            continue

        path_name = package_path.rsplit(marker, 1)[1]
        name = metadata.get("name") or path_name
        if not name:
            continue

        dependencies.add((name, str(version)))

    return dependencies


def load_dependencies_v1(lock: dict):
    dependencies = set()

    def visit(items):
        if not isinstance(items, dict):
            return

        for name, metadata in items.items():
            if not isinstance(metadata, dict):
                continue

            version = metadata.get("version")
            if version:
                dependencies.add((name, str(version)))

            visit(metadata.get("dependencies"))

    visit(lock.get("dependencies"))

    return dependencies


def load_dependencies(package_lock: Path):
    try:
        with package_lock.open(encoding="utf-8") as f:
            lock = json.load(f)

    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {package_lock}: {exc}") from exc

    dependencies = load_dependencies_v2_v3(lock)

    if not dependencies:
        dependencies = load_dependencies_v1(lock)

    return sorted(
        dependencies,
        key=lambda item: (
            item[0].lower(),
            item[1],
        ),
    )


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

    npmrc.write_text(content, encoding="utf-8")
    npmrc.chmod(0o600)


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
    ]

    run_command(
        command,
        env=env,
        timeout=timeout,
        capture_output=False,
    )


def main():
    args = parse_args()

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

    args.artifact_keeper = normalize_registry(args.artifact_keeper)
    args.public_registry = normalize_registry(args.public_registry)

    work_dir = args.work_dir
    pack_dir = work_dir / "packages"
    cache_dir = work_dir / "npm-cache"
    resolution_dir = work_dir / "resolution"
    npmrc = work_dir / "artifact-keeper.npmrc"

    work_dir.mkdir(parents=True, exist_ok=True)
    pack_dir.mkdir(parents=True, exist_ok=True)

    # Isolated npm cache
    shutil.rmtree(cache_dir, ignore_errors=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    shutil.rmtree(resolution_dir, ignore_errors=True)

    env = npm_environment(args.ca_bundle)

    ssl_context = create_ssl_context(args.ca_bundle)

    try:
        package_lock = resolve_package_lock(
            args,
            npm,
            resolution_dir,
            cache_dir,
            env,
        )

        dependencies = load_dependencies(package_lock)

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
        for name, version in dependencies:
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
                    spec,
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
                npm_publish(
                    npm,
                    tarball,
                    args.artifact_keeper,
                    npmrc,
                    cache_dir,
                    env,
                    args.npm_timeout,
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

