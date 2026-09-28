#!/usr/bin/env python3

"""
Seed an Artifact Keeper PyPI repository without a Makefile.

Setup:
    python3 -m venv .venv
    .venv/bin/python -m pip install build twine packaging
    export ARTIFACT_KEEPER_TOKEN=...
    export AK_API=https://localhost/pypi/python

Choose exactly one input:
    .venv/bin/python seed-pypi.py --project-dir /path/to/project
    .venv/bin/python seed-pypi.py --requirements-file /path/to/requirements.txt
    .venv/bin/python seed-pypi.py --wheel /path/to/package.whl
    .venv/bin/python seed-pypi.py --dependency 'requests[socks]==2.32.3'

Use --ca-bundle with a PEM bundle containing public roots and your local CA.
Use --python-versions 310 311 312 and --architectures x86_64 aarch64 to
select CPython/manylinux wheel targets. Temporary workspaces are removed
after each run; existing files under --work-dir are never deleted.

Scope:
    Wheel mode validates and uploads one existing wheel without building or
    downloading dependencies. Only .whl files are accepted; .zip files are
    rejected. The source is preserved.

    Project mode builds a wheel and sdist for the running interpreter/host,
    then seeds runtime dependencies. Optional project extras and build-time
    dependencies are not automatically seeded; list these separately in a
    requirements file. Dependency mode accepts pip specifiers and extras.
    Requirements mode preserves nested -r/-c files and relative local paths.

    Dependency downloads require wheels. Source-only dependencies, editable
    installs, VCS projects, other operating systems (Windows/macOS/musllinux),
    other interpreters and lockfile formats need separate build/export steps.
    pip's cross-target flags do not fully emulate target environment markers;
    run in matching interpreters/containers for complete per-target coverage.
    Requirements files can supply their own indexes and direct URLs.

Install from the seeded repository using its /simple/ URL, for example:
    python -m pip install --index-url https://localhost/pypi/python/simple/ requests
    python -m pip install --index-url https://localhost/pypi/python/simple/ -r requirements.txt
    Configure client authentication and CA trust separately if required.

build and twine run through this interpreter's module entry points. This
preserves build isolation and subprocess timeouts without depending on their
in-process APIs. packaging is imported here for distribution name parsing.
"""

import argparse
import base64
import importlib.util
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.tags import parse_tag
from packaging.version import Version
from packaging.utils import canonicalize_name, parse_sdist_filename, parse_wheel_filename


DEFAULT_PYPI_INDEX_URL = "https://pypi.org/simple"
DEFAULT_WORK_DIR = "/tmp/artifact-keeper-python-seed"
DEFAULT_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"

HTTP_TIMEOUT = 30
COMMAND_TIMEOUT = 1800


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return

        for key, value in attrs:
            if key.lower() == "href" and value:
                self.links.append(value)


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Download Python dependencies from the Internet and "
            "publish missing artifacts to Artifact Keeper."
        )
    )

    inputs = parser.add_mutually_exclusive_group(required=True)

    inputs.add_argument(
        "--wheel",
        type=Path,
        help="Existing wheel to upload without building or downloading dependencies (.whl only)",
    )

    inputs.add_argument(
        "--project-dir",
        type=Path,
        help="Python package project (pyproject.toml and/or setup.py)",
    )

    inputs.add_argument(
        "--requirements-file",
        type=Path,
        help="Existing requirements.txt",
    )

    inputs.add_argument(
        "--dependency",
        help=(
            "Single dependency, optionally with a version. "
            "Examples: requests, requests==2.32.3, requests>=2.32"
        ),
    )

    parser.add_argument(
        "--artifact-keeper",
        default=os.environ.get("AK_API"),
        help="Artifact Keeper PyPI upload URL (default: AK_API)",
    )

    parser.add_argument(
        "--pypi-index-url",
        default=DEFAULT_PYPI_INDEX_URL,
        help="Internet PyPI index",
    )

    parser.add_argument(
        "--python-versions",
        nargs="+",
        default=["310"],
        metavar="VERSION",
        help="Target CPython versions without dots (default: 310)",
    )

    parser.add_argument(
        "--architectures",
        nargs="+",
        default=["x86_64", "aarch64"],
        metavar="ARCH",
        help="Target manylinux architectures (default: x86_64 aarch64)",
    )

    parser.add_argument(
        "--manylinux-version",
        default="2_17",
        help="manylinux platform tag (default: 2_17)",
    )

    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path(DEFAULT_WORK_DIR),
        help="Parent for a temporary, isolated workspace; existing files are preserved",
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
        "--command-timeout",
        type=int,
        default=COMMAND_TIMEOUT,
    )

    args = parser.parse_args()

    if not args.artifact_keeper:
        parser.error("--artifact-keeper or AK_API is required")

    for url in (args.artifact_keeper, args.pypi_index_url):
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            parser.error("Repository URLs must be HTTP(S) URLs without credentials, query or fragment")

    if args.artifact_keeper.rstrip("/").endswith("/simple"):
        parser.error("Use the upload URL /pypi/<repository>, without /simple")

    if args.http_timeout <= 0 or args.command_timeout <= 0:
        parser.error("Timeouts must be positive")

    if any(not re.fullmatch(r"3[0-9]{1,2}", version) for version in args.python_versions):
        parser.error("Python versions must use CPython tag notation, for example 310 or 312")

    if not re.fullmatch(r"2_[0-9]+", args.manylinux_version):
        parser.error("manylinux version must look like 2_17")

    if args.dependency is not None:
        if "\n" in args.dependency or "\r" in args.dependency:
            parser.error("--dependency must contain one non-empty requirement")

        try:
            Requirement(args.dependency)
        except InvalidRequirement:
            parser.error("--dependency must be a valid package requirement, for example requests[socks]==2.32.3")

    for name in ("wheel", "project_dir", "requirements_file", "work_dir", "ca_bundle"):
        path = getattr(args, name)
        if path is not None:
            setattr(args, name, path.expanduser().resolve())

    return args


def normalize_registry(url: str) -> str:
    return url.rstrip("/") + "/"


def pypi_environment(ca_bundle: Path):
    env = os.environ.copy()

    # Build backends and upstream downloads do not need upload credentials.
    for name in ("ARTIFACT_KEEPER_TOKEN", "TWINE_USERNAME", "TWINE_PASSWORD"):
        env.pop(name, None)

    env["PIP_CONFIG_FILE"] = os.devnull
    env["PIP_NO_INPUT"] = "1"
    env["TWINE_CERT"] = str(ca_bundle)
    env["PIP_CERT"] = str(ca_bundle)
    env["REQUESTS_CA_BUNDLE"] = str(ca_bundle)
    env["SSL_CERT_FILE"] = str(ca_bundle)
    env["CURL_CA_BUNDLE"] = str(ca_bundle)
    env["PIP_NO_CACHE_DIR"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"

    return env


def run_command(
    command,
    *,
    cwd=None,
    env=None,
    timeout=COMMAND_TIMEOUT,
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

        details = []

        if stdout.strip():
            details.append(f"stdout:\n{stdout.strip()}")

        if stderr.strip():
            details.append(f"stderr:\n{stderr.strip()}")

        message = (
            f"Command failed with exit code {exc.returncode}:\n"
            f"  {' '.join(map(str, command))}"
        )

        if details:
            message += "\n" + "\n".join(details)

        raise RuntimeError(message) from exc


def create_ssl_context(ca_bundle: Path):
    return ssl.create_default_context(cafile=str(ca_bundle))


def artifact_exists(
    ak_api: str,
    token: str,
    normalized_name: str,
    filename: str,
    ssl_context,
    timeout: int,
) -> bool:
    url = f"{ak_api}simple/{normalized_name}/"

    credentials = base64.b64encode(
        f"__token__:{token}".encode()
    ).decode()

    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Basic {credentials}",
            "Accept": "text/html",
        },
    )

    try:
        with urllib.request.urlopen(
            request,
            context=ssl_context,
            timeout=timeout,
        ) as response:
            html = response.read().decode("utf-8", errors="replace")

    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False

        raise RuntimeError(
            f"Artifact Keeper returned HTTP "
            f"{exc.code} for {url}"
        ) from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Cannot contact Artifact Keeper: {exc}"
        ) from exc

    parser = LinkParser()
    parser.feed(html)

    for href in parser.links:
        parsed = urllib.parse.urlparse(href)

        link_filename = urllib.parse.unquote(
            parsed.path.rsplit("/", 1)[-1]
        )

        if link_filename == filename:
            return True

    return False


def parse_distribution(filename: str):
    if filename.endswith(".whl"):
        name, version, _, _ = parse_wheel_filename(filename)

    elif filename.endswith(".tar.gz") or filename.endswith(".zip"):
        name, version = parse_sdist_filename(filename)

    else:
        raise ValueError(f"Unsupported distribution format: {filename}")

    return (
        name,
        canonicalize_name(name),
        version,
    )


def extract_metadata(wheel: Path):
    with zipfile.ZipFile(wheel) as archive:
        metadata_paths = [
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/METADATA")
        ]

        if len(metadata_paths) != 1:
            raise RuntimeError(
                f"Expected one METADATA file in {wheel}, "
                f"found {len(metadata_paths)}"
            )

        message = BytesParser(
            policy=policy.default
        ).parsebytes(
            archive.read(metadata_paths[0])
        )

    package_name = message["Name"]
    package_version = message["Version"]

    if not package_name or not package_version:
        raise RuntimeError(
            f"Missing Name or Version in {wheel}"
        )

    normalized_name = canonicalize_name(package_name)
    requirements = message.get_all("Requires-Dist") or []

    return (
        package_name,
        normalized_name,
        package_version,
        requirements,
    )


def build_project(
    python: str,
    project_dir: Path,
    dist_dir: Path,
    pypi_index_url: str,
    env,
    timeout: int,
):
    print("==> Building project")
    print(f"    Project: {project_dir}")
    print()

    build_env = env.copy()
    build_env["PIP_INDEX_URL"] = pypi_index_url
    build_env["PIP_EXTRA_INDEX_URL"] = ""

    run_command(
        [
            python,
            "-m",
            "build",
            "--outdir",
            str(dist_dir),
            str(project_dir),
        ],
        env=build_env,
        timeout=timeout,
    )

    print()
    print("==> Checking built distributions")

    distributions = sorted(dist_dir.iterdir())

    if not distributions:
        raise RuntimeError("No distribution was produced")

    run_command(
        [
            python,
            "-m",
            "twine",
            "check",
            *[str(distribution) for distribution in distributions],
        ],
        env=env,
        timeout=timeout,
    )

    wheels = [
        path
        for path in distributions
        if path.suffix == ".whl"
    ]

    if not wheels:
        raise RuntimeError("No wheel was produced")

    return wheels[0]


def download_dependencies(
    python: str,
    req_file: Path,
    deps_dir: Path,
    pypi_index_url: str,
    python_versions,
    architectures,
    manylinux_version: str,
    env,
    timeout: int,
):
    if not req_file.is_file() or not req_file.stat().st_size:
        print("==> No dependencies to download")
        return

    print("==> Downloading dependencies from Internet")
    print(f"    Requirements: {req_file}")
    print(f"    PyPI: {pypi_index_url}")

    for pyver in python_versions:
        for arch in architectures:
            platform = f"manylinux_{manylinux_version}_{arch}"
            platforms = [platform]

            # A glibc baseline also accepts wheels built for older baselines.
            minor = int(manylinux_version.split("_")[1])
            minimum = 5 if arch in ("x86_64", "i686") else 17
            platforms.extend(
                f"manylinux_2_{version}_{arch}"
                for version in range(minor - 1, minimum - 1, -1)
            )
            for version, alias in ((17, "manylinux2014"), (12, "manylinux2010"), (5, "manylinux1")):
                if minimum <= version <= minor:
                    platforms.append(f"{alias}_{arch}")

            abi = f"cp{pyver}"

            print()
            print("==> Target")
            print(f"    Python:   {pyver}")
            print(f"    ABI:      {abi}")
            print(f"    Platform: {platform}")

            download_env = env.copy()
            download_env["PIP_INDEX_URL"] = pypi_index_url
            download_env["PIP_EXTRA_INDEX_URL"] = ""

            run_command(
                [
                    python,
                    "-m",
                    "pip",
                    "download",
                    "--disable-pip-version-check",
                    "--no-cache-dir",
                    "--only-binary=:all:",
                    "--implementation",
                    "cp",
                    "--python-version",
                    pyver,
                    "--abi",
                    abi,
                    *[
                        argument
                        for target in platforms
                        for argument in ("--platform", target)
                    ],
                    "--dest",
                    str(deps_dir),
                    "-r",
                    str(req_file),
                ],
                cwd=req_file.parent,
                env=download_env,
                timeout=timeout,
            )

    print()
    print("==> Downloaded dependency artifacts:")

    for path in sorted(deps_dir.iterdir()):
        if path.is_file():
            print(f"    {path.name}")



def upload_artifacts(
    python: str,
    files,
    ak_api: str,
    token: str,
    ssl_context,
    env,
    http_timeout: int,
    command_timeout: int,
    label: str,
):
    uploaded = 0
    skipped = 0
    failed = 0

    upload_url = ak_api

    for path in files:
        filename = path.name

        try:
            name, normalized_name, version = parse_distribution(filename)

        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            failed += 1
            continue

        print("-" * 60)
        print(f"{label}: {name}=={version}")
        print(f"Artifact: {filename}")

        try:
            exists = artifact_exists(
                ak_api,
                token,
                normalized_name,
                filename,
                ssl_context,
                http_timeout,
            )

        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            failed += 1
            print()
            continue

        if exists:
            print(f"SKIP: {filename} already exists in Artifact Keeper.")
            skipped += 1
            print()
            continue

        print(f"MISSING: {filename}")
        print("Uploading to Artifact Keeper...")

        try:
            twine_env = env.copy()
            twine_env["TWINE_USERNAME"] = "__token__"
            twine_env["TWINE_PASSWORD"] = token

            run_command(
                [
                    python,
                    "-m",
                    "twine",
                    "upload",
                    "--non-interactive",
                    "--repository-url",
                    upload_url,
                    str(path),
                ],
                env=twine_env,
                timeout=command_timeout,
                capture_output=False,
            )

        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            failed += 1
            print()
            continue

        print(f"UPLOADED: {filename}")
        uploaded += 1
        print()

    return uploaded, skipped, failed



def prepare_local_wheel(source: Path, dist_dir: Path):
    if not source.is_file():
        raise RuntimeError(f"Wheel does not exist: {source}")

    filename = source.name
    if not filename.endswith(".whl"):
        raise ValueError("--wheel expects a .whl file; .zip files are not accepted")

    name, version, _, tags = parse_wheel_filename(filename)
    target = dist_dir / filename
    shutil.copyfile(source, target)

    with zipfile.ZipFile(target) as archive:
        wheels = [entry for entry in archive.namelist() if entry.endswith(".dist-info/WHEEL")]
        if len(wheels) != 1:
            raise RuntimeError("Expected one .dist-info/WHEEL; ZIP wrappers containing another wheel are not supported")
        prefix = wheels[0].rsplit("/", 1)[0]
        if prefix + "/METADATA" not in archive.namelist() or prefix + "/RECORD" not in archive.namelist():
            raise RuntimeError("Wheel is missing METADATA or RECORD")
        bad_member = archive.testzip()
        if bad_member:
            raise RuntimeError(f"Corrupt wheel member: {bad_member}")
        message = BytesParser(policy=policy.default).parsebytes(archive.read(wheels[0]))

    package_name, _, package_version, _ = extract_metadata(target)
    if canonicalize_name(package_name) != name or Version(package_version) != version:
        raise RuntimeError("Wheel filename does not match its package metadata")
    metadata_tags = set()
    for tag in message.get_all("Tag", []):
        metadata_tags.update(parse_tag(tag))
    if metadata_tags != tags:
        raise RuntimeError("Wheel filename tags do not match its WHEEL metadata")

    return target


def print_summary(total, uploaded, skipped, failed):
    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"Total:    {total}")
    print(f"Uploaded: {uploaded}")
    print(f"Skipped:  {skipped}")
    print(f"Failed:   {failed}")


def main():
    args = parse_args()

    token = os.environ.get("ARTIFACT_KEEPER_TOKEN")
    if not token:
        print("ERROR: ARTIFACT_KEEPER_TOKEN is not set", file=sys.stderr)
        return 1

    if not args.ca_bundle.is_file():
        print(f"ERROR: CA bundle does not exist: {args.ca_bundle}", file=sys.stderr)
        return 1

    modules = ["twine"]
    if not args.wheel:
        modules.append("pip")
    if args.project_dir:
        modules.append("build")

    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    if missing:
        print(f"ERROR: Missing Python packages: {' '.join(missing)}", file=sys.stderr)
        print("Install them with: python -m pip install build twine packaging", file=sys.stderr)
        return 1

    try:
        args.work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="run-", dir=args.work_dir) as directory:
            return seed(args, token, Path(directory))

    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def seed(args, token: str, work_dir: Path):
    python = sys.executable

    args.artifact_keeper = normalize_registry(args.artifact_keeper)

    dist_dir = work_dir / "dist"
    deps_dir = work_dir / "dependencies"
    req_file = work_dir / "requirements.txt"

    work_dir.mkdir(parents=True, exist_ok=True)
    dist_dir.mkdir(parents=True, exist_ok=True)
    deps_dir.mkdir(parents=True, exist_ok=True)

    env = pypi_environment(args.ca_bundle)

    ssl_context = create_ssl_context(args.ca_bundle)

    if args.wheel:
        print("==> Mode: local wheel")
        print(f"    Wheel: {args.wheel}")
        print()

        wheel = prepare_local_wheel(args.wheel, dist_dir)
        print("==> Checking wheel distribution")
        run_command(
            [python, "-m", "twine", "check", str(wheel)],
            env=env,
            timeout=args.command_timeout,
        )
        uploaded, skipped, failed = upload_artifacts(
            python,
            [wheel],
            args.artifact_keeper,
            token,
            ssl_context,
            env,
            args.http_timeout,
            args.command_timeout,
            "Wheel",
        )
        print_summary(uploaded + skipped + failed, uploaded, skipped, failed)
        return 1 if failed else 0

    project_files = []

    # Determine the mode and prepare the requirements file.
    if args.project_dir:
        if not args.project_dir.is_dir():
            print(f"ERROR: PROJECT_DIR does not exist: {args.project_dir}", file=sys.stderr)
            return 1

        has_build_config = (
            (args.project_dir / "pyproject.toml").is_file()
            or (args.project_dir / "setup.py").is_file()
        )

        if not has_build_config:
            print("ERROR: PROJECT_DIR must contain 'pyproject.toml' or 'setup.py'", file=sys.stderr)
            return 1

        print("==> Mode: Python package")
        print(f"    Project: {args.project_dir}")
        print()

        try:
            wheel = build_project(
                python,
                args.project_dir,
                dist_dir,
                args.pypi_index_url,
                env,
                args.command_timeout,
            )

        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        print()
        print("==> Reading package metadata")

        try:
            package_name, _, package_version, requirements = extract_metadata(wheel)

        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

        print(f"Package: {package_name}=={package_version}")
        print(f"Runtime dependencies: {len(requirements)}")
        print()
        print("==> Runtime requirements")

        if requirements:
            req_file.write_text(
                "".join(f"{requirement}\n" for requirement in requirements),
                encoding="utf-8",
            )

            for requirement in requirements:
                print(f"    {requirement}")
        else:
            req_file.write_text("", encoding="utf-8")
            print("    No runtime dependencies")

        print()

    elif args.requirements_file:
        if not args.requirements_file.is_file():
            print(f"ERROR: REQUIREMENTS_FILE does not exist: {args.requirements_file}", file=sys.stderr)
            return 1

        print("==> Mode: requirements only")
        print(f"    Requirements: {args.requirements_file}")
        print()

        # Keep nested -r/-c includes and relative paths beside their source.
        req_file = args.requirements_file

    else:
        print("==> Mode: single dependency")
        print(f"    Dependency: {args.dependency}")
        print()

        req_file.write_text(f"{args.dependency}\n", encoding="utf-8")


    try:
        download_dependencies(
            python,
            req_file,
            deps_dir,
            args.pypi_index_url,
            args.python_versions,
            args.architectures,
            args.manylinux_version,
            env,
            args.command_timeout,
        )

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    dependency_files = sorted(
        path
        for path in deps_dir.iterdir()
        if path.is_file()
    )

    print()
    print("==> Uploading dependencies to Artifact Keeper")
    print(f"    Repository: {args.artifact_keeper}")

    if not dependency_files:
        print("    No dependency artifacts to upload")

    dep_uploaded, dep_skipped, dep_failed = upload_artifacts(
        python,
        dependency_files,
        args.artifact_keeper,
        token,
        ssl_context,
        env,
        args.http_timeout,
        args.command_timeout,
        "Dependency",
    )

    if args.project_dir:
        print()
        print("==> Publishing project artifacts")
        print(f"    Repository: {args.artifact_keeper}")

        project_files = sorted(
            path
            for path in dist_dir.iterdir()
            if path.is_file()
        )

        if not project_files:
            print("ERROR: No project distributions were found", file=sys.stderr)
            return 1

    pkg_uploaded, pkg_skipped, pkg_failed = upload_artifacts(
        python,
        project_files,
        args.artifact_keeper,
        token,
        ssl_context,
        env,
        args.http_timeout,
        args.command_timeout,
        "Project",
    )

    total = (
        dep_uploaded
        + dep_skipped
        + dep_failed
        + pkg_uploaded
        + pkg_skipped
        + pkg_failed
    )
    uploaded = dep_uploaded + pkg_uploaded
    skipped = dep_skipped + pkg_skipped
    failed = dep_failed + pkg_failed

    print_summary(total, uploaded, skipped, failed)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
