#!/usr/bin/env python3

"""
Seed an Artifact Keeper Maven repository using Python 3.9+ and Maven 3.6.3+.

    export ARTIFACT_KEEPER_TOKEN=...
    export AK_API=https://localhost/maven/maven-external
    python3 seed-maven.py --pom-xml-file /path/to/pom.xml
    python3 seed-maven.py --jar library.jar --pom-file pom.xml \
        --artifact-keeper https://localhost/maven/maven-internal

POM mode resolves into a temporary Maven cache and uploads original artifacts
and POMs, including transitive dependencies and build plugins. JAR mode never
builds: provide a POM, use the JAR's embedded Maven POM, or supply coordinates.
Existing files warn and skip without overwriting. See README.md for VM setup,
Java CA trust, profiles, release-only scope and offline build validation.
"""

import argparse
import base64
import hashlib
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


DEFAULT_WORK_DIR = "/tmp/artifact-keeper-maven-seed"
DEFAULT_UPSTREAM = "https://repo.maven.apache.org/maven2"
HTTP_TIMEOUT = 60
COMMAND_TIMEOUT = 1800
DEPENDENCY_GOAL = "org.apache.maven.plugins:maven-dependency-plugin:3.11.0:go-offline"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        fp.close()
        raise RuntimeError("Repository redirected a request; use its final URL directly")


def validate_url(value):
    url = urllib.parse.urlsplit(value)
    if (url.scheme not in ("https", "http") or not url.hostname
            or url.username is not None or url.password is not None
            or url.query or url.fragment):
        raise ValueError("Repository URLs must be HTTP(S), without credentials, query or fragment")
    return value.rstrip("/")


def parse_args():
    parser = argparse.ArgumentParser(description="Download Maven dependencies or upload an existing JAR to Artifact Keeper.")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--pom-xml-file", type=Path, help="Project POM to resolve, including transitive dependencies")
    inputs.add_argument("--jar", type=Path, help="Existing JAR; never builds it")
    parser.add_argument("--artifact-keeper", default=os.environ.get("AK_API"), help="Exact destination repository URL (default: AK_API)")
    parser.add_argument("--pom-file", type=Path, help="Published POM for --jar; otherwise use its embedded Maven POM")
    parser.add_argument("--group-id", help="Group ID for a JAR without a POM")
    parser.add_argument("--artifact-id", help="Artifact ID for a JAR without a POM")
    parser.add_argument("--version", help="Release version for a JAR without a POM")
    parser.add_argument("--classifier", help="Optional JAR classifier, for example sources or javadoc")
    parser.add_argument("--upstream-url", default=DEFAULT_UPSTREAM, help="Download mirror for POM mode (default: Maven Central)")
    parser.add_argument("--maven-settings", type=Path, help="Explicit upstream Maven settings instead of the default Central mirror")
    parser.add_argument("--profiles", help="Comma-separated Maven profiles to activate")
    parser.add_argument("--maven-goal", action="append", default=[], help="Additional acquisition goal after go-offline, e.g. verify (repeatable; executes project code)")
    parser.add_argument("--maven", default="mvn", help="Maven executable")
    parser.add_argument("--ca-bundle", type=Path, help="Additional PEM CA bundle for Python uploads; Java trust is configured separately")
    parser.add_argument("--work-dir", type=Path, default=Path(DEFAULT_WORK_DIR), help="Parent for disposable workspaces; existing files are preserved")
    parser.add_argument("--http-timeout", type=int, default=HTTP_TIMEOUT)
    parser.add_argument("--command-timeout", type=int, default=COMMAND_TIMEOUT)
    args = parser.parse_args()
    if not args.artifact_keeper:
        parser.error("--artifact-keeper or AK_API is required")
    try:
        args.artifact_keeper = validate_url(args.artifact_keeper)
        args.upstream_url = validate_url(args.upstream_url)
    except ValueError as exc:
        parser.error(str(exc))
    if min(args.http_timeout, args.command_timeout) <= 0:
        parser.error("Timeouts must be positive")
    if not args.jar and any((args.pom_file, args.group_id, args.artifact_id, args.version, args.classifier)):
        parser.error("POM/coordinate/classifier arguments apply only to --jar")
    if args.jar and (args.profiles or args.maven_goal or args.maven_settings):
        parser.error("Maven settings, profiles and goals apply only to --pom-xml-file")
    if any(goal.startswith("-") for goal in args.maven_goal):
        parser.error("--maven-goal accepts goals, not Maven options")
    for key in ("pom_xml_file", "jar", "pom_file", "maven_settings", "ca_bundle", "work_dir"):
        value = getattr(args, key)
        if value is not None:
            setattr(args, key, value.expanduser().resolve())
    return args


def xml_document(data):
    # Do not accept DTD/entity declarations in artifact metadata.
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("POM must not contain DTD/entity declarations")
    root = ET.fromstring(data)
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    if root.tag != "project":
        raise ValueError("Expected a Maven <project> POM")
    return root


def coordinates(group, artifact, version, classifier=None):
    for name, value in (("groupId", group), ("artifactId", artifact), ("version", version)):
        if not value or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*", value) or ".." in value:
            raise ValueError(f"Invalid or unresolved {name}: {value!r}; supply a resolved, published POM")
    if not all(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", part) for part in group.split(".")):
        raise ValueError("Invalid groupId")
    if version.upper().endswith("-SNAPSHOT"):
        raise ValueError("SNAPSHOT publication is not supported; use a fixed release version")
    if classifier is not None and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", classifier):
        raise ValueError("Invalid classifier")
    return f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}"


def prepare_jar(args):
    if args.jar.suffix != ".jar" or not args.jar.is_file() or not zipfile.is_zipfile(args.jar):
        raise ValueError("--jar must be an existing .jar ZIP archive")
    explicit = (args.group_id, args.artifact_id, args.version)
    if any(explicit) and not all(explicit):
        raise ValueError("Provide all of --group-id, --artifact-id and --version")
    pom = args.pom_file.read_bytes() if args.pom_file else None
    if pom is None and not all(explicit):
        with zipfile.ZipFile(args.jar) as archive:
            names = [name for name in archive.namelist() if re.fullmatch(r"META-INF/maven/[^/]+/[^/]+/pom.xml", name)]
            if len(names) != 1:
                raise ValueError("JAR has no unique embedded Maven POM; provide --pom-file or all three coordinates")
            pom = archive.read(names[0])
    if pom is not None:
        root = xml_document(pom)
        group = root.findtext("groupId") or root.findtext("parent/groupId")
        artifact = root.findtext("artifactId")
        version = root.findtext("version") or root.findtext("parent/version")
        group, artifact, version = (value.strip() if value else value for value in (group, artifact, version))
        if all(explicit) and explicit != (group, artifact, version):
            raise ValueError("Explicit coordinates disagree with the POM")
        if root.findtext("packaging", "jar").strip() not in ("jar", "maven-plugin", "bundle") and not args.classifier:
            raise ValueError("POM packaging does not describe a JAR")
    else:
        group, artifact, version = explicit
        coordinates(group, artifact, version, args.classifier)
        root = ET.Element("project", xmlns="http://maven.apache.org/POM/4.0.0")
        for key, value in (("modelVersion", "4.0.0"), ("groupId", group), ("artifactId", artifact), ("version", version), ("packaging", "jar")):
            ET.SubElement(root, key).text = value
        pom = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        print("WARNING: Generated a minimal POM with no dependencies; use --pom-file to preserve dependencies.")
    base = coordinates(group, artifact, version, args.classifier)
    suffix = f"-{args.classifier}" if args.classifier else ""
    return [(base + ".pom", pom), (base + suffix + ".jar", args.jar.read_bytes())]


class RepositoryClient:
    def __init__(self, args, token):
        self.url = args.artifact_keeper + "/"
        self.timeout = args.http_timeout
        context = ssl.create_default_context()
        if args.ca_bundle:
            context.load_verify_locations(cafile=str(args.ca_bundle))
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
        self.authorization = "Basic " + base64.b64encode(f"__token__:{token}".encode()).decode()

    def request(self, path, data=None):
        headers = {"Authorization": self.authorization, "Content-Type": "application/octet-stream"}
        url = self.url + urllib.parse.quote(path, safe="/")
        request = urllib.request.Request(url, headers=headers, data=data, method="PUT" if data is not None else "GET")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and data is None:
                exc.close()
                return None
            raise


def upload_artifacts(client, artifacts):
    uploaded = skipped = failed = 0
    for path, data in artifacts:
        print("-" * 60)
        print(f"Artifact: {path}")
        try:
            existing = client.request(path)
            if existing is not None:
                print("WARNING: Artifact already exists in Artifact Keeper; keeping the published file.")
                if hashlib.sha256(existing).digest() != hashlib.sha256(data).digest():
                    print("WARNING: Existing content differs; publish a new version to change it.")
                print(f"SKIP: {path}")
                skipped += 1
                continue
            print(f"MISSING: {path}")
            print("Uploading to Artifact Keeper...")
            try:
                client.request(path, data)
            except urllib.error.HTTPError as exc:
                if exc.code != 409 or client.request(path) is None:
                    raise
                exc.close()
                print(f"WARNING: Concurrent publication; SKIP: {path}")
                skipped += 1
                continue
            if client.request(path) != data:
                raise RuntimeError("Uploaded artifact failed read-back verification")
            print(f"UPLOADED: {path}")
            uploaded += 1
        except (OSError, ValueError, RuntimeError) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            print(f"ERROR: {exc}", file=sys.stderr)
            failed += 1
        finally:
            print()
    return uploaded, skipped, failed


def run_command(command, args, env):
    try:
        subprocess.run(command, cwd=args.pom_xml_file.parent, env=env, check=True, timeout=args.command_timeout)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Maven failed with exit code {exc.returncode}; no artifacts uploaded") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Maven timed out after {args.command_timeout}s") from exc


def downloaded_artifacts(repository):
    files = []
    for path in sorted(repository.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(repository)
        parts = relative.parts
        if len(parts) < 4:
            continue
        artifact, version, filename = parts[-3:]
        if filename.startswith(("_", ".", "maven-metadata")) or filename.endswith((".lastUpdated", ".sha1", ".sha256", ".sha512", ".md5", ".asc", ".part", ".lock")):
            continue
        coordinates(".".join(parts[:-3]), artifact, version)
        if not filename.startswith(f"{artifact}-{version}.") and not filename.startswith(f"{artifact}-{version}-"):
            raise ValueError(f"Unexpected artifact in Maven cache: {relative}")
        files.append((relative.as_posix(), path))
    if not files:
        raise RuntimeError("Maven did not download any artifacts")
    return files


def download_dependencies(args, work):
    if not args.pom_xml_file.is_file():
        raise ValueError(f"POM does not exist: {args.pom_xml_file}")
    xml_document(args.pom_xml_file.read_bytes())
    if not shutil.which(args.maven):
        raise ValueError(f"Maven executable not found: {args.maven}")
    repository = work / "repository"
    empty = work / "global-settings.xml"
    empty.write_text("<settings/>")
    settings = args.maven_settings
    if settings is None:
        settings = work / "settings.xml"
        root = ET.Element("settings")
        mirror = ET.SubElement(ET.SubElement(root, "mirrors"), "mirror")
        for key, value in (("id", "seed-upstream"), ("mirrorOf", "*"), ("url", args.upstream_url)):
            ET.SubElement(mirror, key).text = value
        ET.ElementTree(root).write(settings, encoding="utf-8", xml_declaration=True)
    env = os.environ.copy()
    for key in ("ARTIFACT_KEEPER_TOKEN", "AK_API", "MAVEN_ARGS"):
        env.pop(key, None)
    command = [args.maven, "-B", "-ntp", "-C", "-gs", str(empty), "-s", str(settings),
               f"-Dmaven.repo.local={repository}", "-f", str(args.pom_xml_file)]
    if args.profiles:
        command += ["-P", args.profiles]
    print("==> Downloading dependencies from Internet", flush=True)
    print(f"    POM: {args.pom_xml_file}", flush=True)
    run_command(command + [DEPENDENCY_GOAL], args, env)
    if args.maven_goal:
        run_command(command + args.maven_goal, args, env)
    return downloaded_artifacts(repository)


def print_summary(uploaded, skipped, failed):
    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"Total:    {uploaded + skipped + failed}")
    print(f"Uploaded: {uploaded}")
    print(f"Skipped:  {skipped}")
    print(f"Failed:   {failed}")


def main():
    args = parse_args()
    try:
        token = os.environ.get("ARTIFACT_KEEPER_TOKEN")
        if not token:
            raise ValueError("ARTIFACT_KEEPER_TOKEN is not set")
        client = RepositoryClient(args, token)
        print(f"==> Mode: {'existing JAR' if args.jar else 'POM dependencies'}")
        print(f"    Repository: {args.artifact_keeper}")
        print()
        args.work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="run-", dir=args.work_dir) as directory:
            if args.jar:
                artifacts = prepare_jar(args)
            else:
                files = download_dependencies(args, Path(directory))
                artifacts = ((relative, path.read_bytes()) for relative, path in files)
            print("==> Uploading artifacts to Artifact Keeper")
            counts = upload_artifacts(client, artifacts)
        print_summary(*counts)
        return 1 if counts[2] else 0
    except (OSError, ValueError, RuntimeError, ET.ParseError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print_summary(0, 0, 1)
        return 1


if __name__ == "__main__":
    sys.exit(main())
