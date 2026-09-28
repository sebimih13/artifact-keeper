#!/usr/bin/env python3

"""
Generate an Eclipse P2 update site from features/ and plugins/, then publish it.
Uses Eclipse's FeaturesAndBundlesPublisher, not hand-written OSGi metadata.
Python 3.9+ (standard library only) and Eclipse with the P2 publisher are required.

Example:
    python3 seed-p2.py --source-dir /path/to/input --output-dir /path/to/site \
        --eclipse /path/to/eclipse --generate-only

For uploading, set ARTIFACT_KEEPER_TOKEN and:
    AK_API=https://localhost/api/v1/repositories/p2
Use a local Generic repository with artifact versioning enabled. The Eclipse
update-site URL is AK_API/download/ (optionally followed by --site-path).

Each run regenerates the complete repository from the supplied JARs. Keep all
versions you want advertised in the source directories. Changed versioned JARs
are rejected; publish a new bundle/feature version instead. New artifacts are
uploaded before metadata, and unchanged bytes are skipped. Metadata replacement
across multiple files is not atomic: serialize writers and use immutable release
sites/composite repositories when readers must never see a mixed generation.
See README.md for setup, versioning, categories, TLS and update examples.
"""

import argparse
import collections
import hashlib
import json
import lzma
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
import xml.etree.ElementTree as ET
import zipfile

from pathlib import Path


DEFAULT_WORK_DIR = "/tmp/artifact-keeper-p2-seed"
DEFAULT_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
HTTP_TIMEOUT = 60
COMMAND_TIMEOUT = 1800
METADATA_FILES = ("artifacts.jar", "artifacts.xml.xz", "content.jar", "content.xml.xz", "p2.index")
P2_INDEX = (
    "version=1\n"
    "metadata.repository.factory.order=content.xml.xz,content.xml,!\n"
    "artifact.repository.factory.order=artifacts.xml.xz,artifacts.xml,!\n"
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        fp.close()
        raise RuntimeError("Repository redirected the request; use its final API URL")


def parse_args():
    parser = argparse.ArgumentParser(description="Generate and upload a P2 site from feature and plugin JARs.")
    parser.add_argument("--source-dir", "--project-dir", type=Path, required=True, help="Directory containing features/ and plugins/")
    parser.add_argument("--output-dir", type=Path, required=True, help="Separate directory for the complete generated site")
    parser.add_argument("--eclipse", default=os.environ.get("ECLIPSE", "eclipse"), help="Eclipse executable with P2 publisher (default: ECLIPSE or eclipse)")
    parser.add_argument("--artifact-keeper", default=os.environ.get("AK_API"), help="https://host/api/v1/repositories/<key> (default: AK_API)")
    parser.add_argument("--site-path", default="", help="Optional repository-relative site directory, e.g. releases/1.0")
    parser.add_argument("--generate-only", action="store_true", help="Generate local files without contacting Artifact Keeper")
    parser.add_argument("--ca-bundle", type=Path, default=Path(DEFAULT_CA_BUNDLE))
    parser.add_argument("--work-dir", type=Path, default=Path(DEFAULT_WORK_DIR))
    parser.add_argument("--http-timeout", type=int, default=HTTP_TIMEOUT)
    parser.add_argument("--command-timeout", type=int, default=COMMAND_TIMEOUT)
    args = parser.parse_args()

    for key in ("source_dir", "output_dir", "ca_bundle", "work_dir"):
        setattr(args, key, getattr(args, key).expanduser().resolve())
    if args.source_dir.is_relative_to(args.output_dir) or args.output_dir.is_relative_to(args.source_dir):
        parser.error("Source and output must be separate, non-overlapping directories")
    for name in ("features", "plugins"):
        if not (args.source_dir / name).is_dir():
            parser.error(f"Missing source directory: {args.source_dir / name}")
    if args.http_timeout <= 0 or args.command_timeout <= 0:
        parser.error("Timeouts must be positive")
    if args.site_path and not re.fullmatch(r"[A-Za-z0-9_-]+(?:[./][A-Za-z0-9_-]+)*", args.site_path):
        parser.error("--site-path must be a relative path without empty, dot or parent segments")
    if not args.generate_only:
        url = urllib.parse.urlsplit(args.artifact_keeper or "")
        if (url.scheme not in ("https", "http") or not url.hostname or url.username or url.password
                or url.query or url.fragment or not re.fullmatch(r"/api/v1/repositories/[A-Za-z0-9_-]+/?", url.path)):
            parser.error("Set AK_API or --artifact-keeper to https://host/api/v1/repositories/<key>")
        args.artifact_keeper = args.artifact_keeper.rstrip("/")
        if not args.ca_bundle.is_file():
            parser.error(f"CA bundle does not exist: {args.ca_bundle}")
    return args


def sha256(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_source(source: Path, target: Path):
    hashes = collections.Counter()
    for name in ("features", "plugins"):
        (target / name).mkdir(parents=True)
        for path in sorted((source / name).iterdir()):
            if path.name.startswith("."):
                continue
            if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".jar":
                raise RuntimeError(f"Expected flat JAR files, not directories/symlinks/other files: {path}")
            if not zipfile.is_zipfile(path):
                raise RuntimeError(f"Not a valid JAR: {path}")
            with zipfile.ZipFile(path) as archive:
                required = "feature.xml" if name == "features" else "META-INF/MANIFEST.MF"
                if required not in archive.namelist():
                    raise RuntimeError(f"{path} is missing {required}")
            copied = target / name / path.name
            shutil.copy2(path, copied)
            hashes[sha256(copied)] += 1
    if not hashes:
        raise RuntimeError("No feature/plugin JARs were found")
    return hashes


def run_publisher(args, executable: Path, source: Path, site: Path, work: Path):
    print("==> Generating P2 metadata with Eclipse")
    installation = executable.parent
    configuration = installation / "configuration"
    if not (configuration / "config.ini").is_file():
        raise RuntimeError("--eclipse must point to an Eclipse installation executable with configuration/config.ini")
    private_configuration = work / "configuration"
    private_configuration.mkdir()
    shutil.copy2(configuration / "config.ini", private_configuration / "config.ini")
    bundles = configuration / "org.eclipse.equinox.simpleconfigurator" / "bundles.info"
    if bundles.is_file():
        (private_configuration / bundles.parent.name).mkdir()
        shutil.copy2(bundles, private_configuration / bundles.parent.name / bundles.name)

    command = [
        str(executable), "-nosplash", "--launcher.suppressErrors", "-consolelog",
        "-configuration", str(private_configuration), "-data", str(work / "workspace"),
        "-application", "org.eclipse.equinox.p2.publisher.FeaturesAndBundlesPublisher",
        "-metadataRepository", site.as_uri(), "-artifactRepository", site.as_uri(),
        "-metadataRepositoryName", "Artifact Keeper P2",
        "-artifactRepositoryName", "Artifact Keeper P2",
        "-source", str(source), "-compress", "-publishArtifacts",
        "-vmargs", f"-Declipse.p2.data.area={(work / 'p2-data').as_uri()}",
    ]
    env = os.environ.copy()
    env.pop("ARTIFACT_KEEPER_TOKEN", None)
    try:
        result = subprocess.run(command, env=env, timeout=args.command_timeout, check=True, text=True, capture_output=True)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Eclipse publisher timed out after {args.command_timeout}s") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Eclipse publisher failed:\n{exc.stdout}\n{exc.stderr}") from exc
    for filename in ("content.jar", "artifacts.jar"):
        if not (site / filename).is_file():
            raise RuntimeError(f"Eclipse did not generate {filename}:\n{result.stdout}\n{result.stderr}")


def read_metadata(path: Path, name: str):
    with zipfile.ZipFile(path) as archive:
        try:
            data = archive.read(name + ".xml")
        except KeyError as exc:
            raise RuntimeError(f"Missing {name}.xml in {path}") from exc
    root = ET.fromstring(data)
    if root.tag != "repository":
        raise RuntimeError(f"Invalid P2 metadata: {path}")
    return data


def without_timestamp(data: bytes):
    # Preserve the publisher's XML, including its P2 processing instruction.
    return re.sub(rb"(<property\s+name=['\"]p2.timestamp['\"]\s+value=['\"])[0-9]+(['\"]\s*/>)", rb"\g<1>0\2", data)


def package_metadata(site: Path, previous: Path):
    for name in ("artifacts", "content"):
        xml = read_metadata(site / (name + ".jar"), name)
        old = previous / (name + ".jar")
        if old.is_file():
            old_xml = read_metadata(old, name)
            if without_timestamp(xml) == without_timestamp(old_xml):
                xml = old_xml
        info = zipfile.ZipInfo(name + ".xml", date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        with zipfile.ZipFile(site / (name + ".jar"), "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            archive.writestr(info, xml)
        (site / (name + ".xml.xz")).write_bytes(lzma.compress(xml, format=lzma.FORMAT_XZ, preset=6))
    (site / "p2.index").write_text(P2_INDEX, encoding="utf-8")


def generate_site(args, executable: Path, work: Path):
    source = work / "source"
    expected = snapshot_source(args.source_dir, source)
    site = work / "site"
    site.mkdir()
    run_publisher(args, executable, source, site, work)
    artifacts = sorted(path for name in ("features", "plugins") for path in (site / name).glob("*.jar"))
    if collections.Counter(sha256(path) for path in artifacts) != expected:
        raise RuntimeError("Publisher did not copy every input JAR unchanged. Check invalid bundles/features or duplicate IDs/versions; metadata was not uploaded.")
    package_metadata(site, args.output_dir)
    paths = [path.relative_to(site) for path in artifacts] + [Path(name) for name in METADATA_FILES]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for relative in paths:
        target = args.output_dir / relative
        if target.parent.is_symlink():
            raise RuntimeError(f"Refusing to write through symlink: {target.parent}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise RuntimeError(f"Refusing to replace symlink: {target}")
        # Only replace generated paths, never recursively delete the user's directory.
        with tempfile.NamedTemporaryFile(prefix=".seed-", dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
        try:
            shutil.copyfile(site / relative, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    print(f"    Generated site: {args.output_dir}")
    return paths


def request(opener, url, token, timeout, *, method="GET", path=None):
    headers = {"Authorization": f"Bearer {token}"}
    if path is None:
        return opener.open(urllib.request.Request(url, headers=headers, method=method), timeout=timeout)
    content_type = "application/x-xz" if path.name.endswith(".xz") else "text/plain" if path.name == "p2.index" else "application/java-archive"
    headers.update({"Content-Type": content_type, "Content-Length": str(path.stat().st_size), "X-Checksum-Sha256": sha256(path)})
    with path.open("rb") as stream:
        response = opener.open(urllib.request.Request(url, data=stream, headers=headers, method=method), timeout=timeout)
    return response


def remote_digest(opener, url, token, timeout):
    try:
        with request(opener, url, token, timeout) as response:
            digest = hashlib.sha256()
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                digest.update(chunk)
            return digest.hexdigest()
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 404:
            return None
        raise RuntimeError(f"HTTP {exc.code} checking {url}") from exc


def upload_site(args, paths, token):
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(args.ca_bundle))))
    with request(opener, args.artifact_keeper, token, args.http_timeout) as response:
        repository = json.load(response)
    if repository.get("format") != "generic" or repository.get("repo_type") != "local" or not repository.get("versioning_enabled"):
        raise RuntimeError("Use a local Generic repository with artifact versioning enabled so metadata can be replaced safely")

    prefix = args.site_path + "/" if args.site_path else ""
    print("==> Checking existing artifacts")
    plan = []
    for relative in paths:
        encoded = urllib.parse.quote(prefix + relative.as_posix(), safe="/")
        local = args.output_dir / relative
        existing = remote_digest(opener, args.artifact_keeper + "/download/" + encoded, token, args.http_timeout)
        same = existing == sha256(local)
        if existing and not same and relative.parts[0] in ("features", "plugins"):
            raise RuntimeError(f"Existing JAR differs: {relative}. Increment the bundle/feature version instead of replacing it.")
        plan.append((relative, encoded, same))

    uploaded = skipped = failed = 0
    for index, (relative, encoded, same) in enumerate(plan):
        print("-" * 60)
        print(f"Artifact: {relative.as_posix()}")
        if same:
            print(f"SKIP: {relative} already exists in Artifact Keeper.")
            skipped += 1
            continue
        print(f"MISSING/UPDATED: {relative}")
        print("Uploading to Artifact Keeper...")
        try:
            with request(opener, args.artifact_keeper + "/artifacts/" + encoded, token, args.http_timeout, method="PUT", path=args.output_dir / relative) as response:
                if response.status not in (200, 201, 202, 204):
                    raise RuntimeError(f"Unexpected upload status: {response.status}")
            if remote_digest(opener, args.artifact_keeper + "/download/" + encoded, token, args.http_timeout) != sha256(args.output_dir / relative):
                raise RuntimeError(f"Uploaded bytes failed verification: {relative}")
            print(f"UPLOADED: {relative}")
            uploaded += 1
        except (OSError, RuntimeError) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            print(f"ERROR: {exc}", file=sys.stderr)
            failed += len(plan) - index
            print("ERROR: Remaining uploads withheld; rerun after resolving the failure", file=sys.stderr)
            break
        print()
    print_summary(uploaded + skipped + failed, uploaded, skipped, failed)
    print(f"Update site: {args.artifact_keeper}/download/{urllib.parse.quote(prefix, safe='/')}")
    return 1 if failed else 0


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
    try:
        executable = shutil.which(args.eclipse)
        if not executable:
            raise RuntimeError("Eclipse executable not found; set --eclipse or ECLIPSE")
        token = os.environ.get("ARTIFACT_KEEPER_TOKEN")
        if not args.generate_only and not token:
            raise RuntimeError("ARTIFACT_KEEPER_TOKEN is not set")
        args.work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="run-", dir=args.work_dir) as directory:
            paths = generate_site(args, Path(executable).resolve(), Path(directory))
        if args.generate_only:
            print(f"Generated: {len(paths)} files (no upload requested)")
            return 0
        return upload_site(args, paths, token)
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, ET.ParseError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
