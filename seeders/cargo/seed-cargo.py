#!/usr/bin/env python3

"""
Seed Artifact Keeper Cargo repositories using Python 3.11+ and Cargo.

Set ARTIFACT_KEEPER_TOKEN and AK_API=https://localhost (server base URL).
Choose exactly one input:
    python3 seed-cargo.py --toml-file /path/to/Cargo.toml
    python3 seed-cargo.py --dependency serde
    python3 seed-cargo.py --dependency serde@1.0.228
    python3 seed-cargo.py --project-dir /path/to/internal-crate

External modes fetch the Cargo.lock dependency closure from crates.io and mirror
original .crate bytes into cargo-external. Existing locks are used with --locked;
a missing lock is generated beside the workspace manifest. A dependencies-only
TOML fragment is also accepted and resolved inside a temporary wrapper project.
Project mode checks for an existing version, then uses cargo publish --registry
cargo-internal with normal package/build verification. Reads use the virtual
cargo repository. Duplicates warn and skip successfully. No dependencies are
published automatically in project mode; seed external dependencies first.

Cargo runs with isolated configuration/cache, ignoring project/global source
replacement and registry settings. Standard aliases cargo, cargo-internal and
cargo-external are configured explicitly. Git dependencies and third-party
registries cannot be mirrored by this script. See README.md for details.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_WORK_DIR = "/tmp/artifact-keeper-cargo-seed"
DEFAULT_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
HTTP_TIMEOUT = 60
COMMAND_TIMEOUT = 1800
CRATES_IO_SOURCE = "registry+https://github.com/rust-lang/crates.io-index"
CRATES_IO_INDEX = "https://index.crates.io/"
CRATE_NAME = r"[A-Za-z][A-Za-z0-9_-]*"
EXACT_VERSION = r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        fp.close()
        raise RuntimeError("Registry redirected a request; configure its final URL")


def parse_args():
    parser = argparse.ArgumentParser(description="Mirror crates.io dependencies or publish an internal Cargo crate.")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--toml-file", type=Path, help="Cargo.toml or a dependencies-only TOML fragment")
    inputs.add_argument("--project-dir", type=Path, help="Internal crate/workspace directory containing Cargo.toml")
    inputs.add_argument("--dependency", help="crates.io name or name@exact-version")
    parser.add_argument("--artifact-keeper", default=os.environ.get("AK_API", "https://localhost"), help="Server base URL (default: AK_API or https://localhost)")
    parser.add_argument("--external-repo", default="cargo-external")
    parser.add_argument("--internal-repo", default="cargo-internal")
    parser.add_argument("--virtual-repo", default="cargo")
    parser.add_argument("--package", help="Workspace package to publish (project mode only)")
    parser.add_argument("--allow-dirty", action="store_true", help="Allow cargo publish to package uncommitted files")
    parser.add_argument("--cargo", default="cargo", help="Cargo executable")
    parser.add_argument("--ca-bundle", type=Path, default=Path(DEFAULT_CA_BUNDLE), help="Additional PEM CA bundle; system roots are retained")
    parser.add_argument("--work-dir", type=Path, default=Path(DEFAULT_WORK_DIR))
    parser.add_argument("--http-timeout", type=int, default=HTTP_TIMEOUT)
    parser.add_argument("--command-timeout", type=int, default=COMMAND_TIMEOUT)
    args = parser.parse_args()
    url = urllib.parse.urlsplit(args.artifact_keeper)
    if (url.scheme not in ("http", "https") or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment or url.path not in ("", "/")):
        parser.error("AK_API/--artifact-keeper must be a server base URL, e.g. https://localhost")
    args.artifact_keeper = args.artifact_keeper.rstrip("/")
    for key in ("external_repo", "internal_repo", "virtual_repo"):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", getattr(args, key)):
            parser.error("Repository keys must contain only letters, digits, underscores or hyphens")
    if args.dependency and not re.fullmatch(CRATE_NAME + r"(?:@" + EXACT_VERSION + r")?", args.dependency):
        parser.error("Use a crate name or name@exact-version, for example serde@1.0.228")
    if (args.package or args.allow_dirty) and not args.project_dir:
        parser.error("--package and --allow-dirty apply only to --project-dir")
    if min(args.http_timeout, args.command_timeout) <= 0:
        parser.error("Timeouts must be positive")
    for key in ("toml_file", "project_dir", "ca_bundle", "work_dir"):
        if getattr(args, key) is not None:
            setattr(args, key, getattr(args, key).expanduser().resolve())
    return args


def run_command(command, args, work, env, capture_output=True):
    try:
        return subprocess.run(
            command, cwd=work, env=env, check=True, text=True,
            timeout=args.command_timeout, capture_output=capture_output,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Cargo failed ({exc.returncode}):\n{exc.stdout or ''}\n{exc.stderr or ''}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Cargo timed out after {args.command_timeout}s") from exc


def index_path(name):
    name = name.lower()
    if len(name) < 3:
        return f"{len(name)}/{name}"
    if len(name) == 3:
        return f"3/{name[0]}/{name}"
    return f"{name[:2]}/{name[2:4]}/{name}"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class RegistryClient:
    def __init__(self, args, token):
        self.args = args
        self.token = token
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=str(args.ca_bundle))
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))

    def request(self, url, *, local=True, data=None):
        headers = {"User-Agent": "artifact-keeper-cargo-seeder/1.0"}
        if local:
            if not url.startswith(self.args.artifact_keeper + "/"):
                raise RuntimeError("Refusing to send registry credentials to a different server")
            headers["Authorization"] = f"Bearer {self.token}"
        if data is not None:
            headers["Content-Type"] = "application/octet-stream"
        for attempt in range(5):
            try:
                request = urllib.request.Request(url, headers=headers, data=data, method="PUT" if data is not None else "GET")
                with self.opener.open(request, timeout=self.args.http_timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or attempt == 4:
                    raise
                retry = exc.headers.get("Retry-After", "60")
                exc.close()
                delay = max(1, int(retry)) if retry.isdigit() else 60
                if delay > 300:
                    raise RuntimeError("Registry requested a long rate-limit wait; rerun later") from exc
                print(f"Rate limited; retrying in {delay}s ({attempt + 1}/4)", flush=True)
                time.sleep(delay)

    def url(self, repo):
        return f"{self.args.artifact_keeper}/cargo/{repo}/"

    def validate_repository(self, repo, kind):
        info = json.loads(self.request(f"{self.args.artifact_keeper}/api/v1/repositories/{repo}"))
        if info.get("format") != "cargo" or info.get("repo_type") != kind:
            raise RuntimeError(f"{repo} must be a {kind} Cargo repository")
        config = json.loads(self.request(self.url(repo) + "config.json"))
        if kind == "local" and config.get("api", "").rstrip("/") != self.url(repo).rstrip("/"):
            raise RuntimeError(f"{repo} advertises an unexpected publish API URL")

    def entry(self, repo, name, version):
        try:
            data = self.request(self.url(repo) + index_path(name))
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 404:
                return None
            raise
        for line in data.splitlines():
            entry = json.loads(line)
            if entry["vers"] == version:
                return entry
        return None


def cargo_environment(args, work, token, publishing):
    env = {key: value for key, value in os.environ.items() if not key.startswith("CARGO_") and key != "ARTIFACT_KEEPER_TOKEN"}
    home = work / "cargo-home"
    home.mkdir()
    ca_file = work / "ca-bundle.pem"
    system_ca = ssl.get_default_verify_paths().cafile
    roots = Path(system_ca).read_bytes() if system_ca else b""
    ca_file.write_bytes(roots + b"\n" + args.ca_bundle.read_bytes())
    config = '[registry]\nglobal-credential-providers = ["cargo:token"]\n'
    for name, repo in (("cargo", args.virtual_repo), ("cargo-internal", args.internal_repo), ("cargo-external", args.external_repo)):
        index = f"sparse+{args.artifact_keeper}/cargo/{repo}/"
        config += f"\n[registries.{name}]\nindex = {json.dumps(index)}\n"
        env[f"CARGO_REGISTRIES_{name.upper().replace('-', '_')}_TOKEN"] = token
    if publishing:
        config += '\n[source.crates-io]\nreplace-with = "cargo"\n'
    (home / "config.toml").write_text(config)
    env.update({"CARGO_HOME": str(home), "CARGO_TARGET_DIR": str(work / "target"), "CARGO_HTTP_CAINFO": str(ca_file), "CARGO_TERM_COLOR": "never"})
    return env


def prepare_manifest(args, work):
    if args.project_dir:
        manifest = args.project_dir / "Cargo.toml"
    elif args.toml_file:
        manifest = args.toml_file
    else:
        name, separator, version = args.dependency.partition("@")
        requirement = "=" + version if separator else "*"
        text = f'[dependencies]\n{name} = {json.dumps(requirement)}\n'
        manifest = work / "dependency.toml"
        manifest.write_text(text)
    if not manifest.is_file():
        raise RuntimeError(f"Manifest does not exist: {manifest}")
    text = manifest.read_text()
    document = tomllib.loads(text)
    if "package" in document or "workspace" in document:
        if manifest.name != "Cargo.toml":
            raise RuntimeError("A full Cargo project/workspace manifest must be named Cargo.toml")
        return manifest
    if args.project_dir:
        raise RuntimeError("Project Cargo.toml must contain [package] or [workspace]")
    if not document or set(document) - {"dependencies", "dev-dependencies", "build-dependencies", "target", "features"}:
        raise RuntimeError("A TOML fragment may contain only dependency, target and feature tables")
    # Relative paths/workspace inheritance require a real Cargo project context.
    if re.search(r"\b(path|workspace)\s*=", text):
        raise RuntimeError("Path/workspace dependencies require a full Cargo.toml project")
    project = work / "resolver"
    project.mkdir()
    (project / "lib.rs").write_text("")
    manifest = project / "Cargo.toml"
    manifest.write_text('[package]\nname = "ak-seed-resolver"\nversion = "0.0.0"\nedition = "2021"\n[lib]\npath = "lib.rs"\n' + text)
    return manifest


def metadata(args, manifest, work, env):
    result = run_command([args.cargo, "metadata", "--no-deps", "--format-version", "1", "--manifest-path", str(manifest)], args, work, env)
    return json.loads(result.stdout)


def upstream_entry(client, name, version):
    data = client.request(CRATES_IO_INDEX + index_path(name), local=False)
    for line in data.splitlines():
        entry = json.loads(line)
        if entry["vers"] == version:
            if entry.get("yanked"):
                raise RuntimeError(f"Refusing to mirror yanked crate {name}@{version}")
            if entry.get("v", 1) > 2:
                raise RuntimeError(f"Unsupported upstream index schema for {name}@{version}")
            return entry
    raise RuntimeError(f"Upstream index is missing {name}@{version}")


def publish_payload(entry, crate):
    # Artifact Keeper accepts index-shaped dependencies directly. Preserve renamed
    # packages, target cfg, dependency kind and optional/default-feature flags.
    # Its current index writer emits 'features', so fold v2 feature definitions
    # into that map; modern Cargo understands their dep:/weak-feature syntax.
    features = dict(entry.get("features", {}))
    features.update(entry.get("features2", {}))
    payload = {
        "name": entry["name"], "vers": entry["vers"], "deps": entry.get("deps", []),
        "features": features, "links": entry.get("links"), "rust_version": entry.get("rust_version"),
    }
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    return struct.pack("<I", len(encoded)) + encoded + struct.pack("<I", len(crate)) + crate


def mirror_dependencies(args, manifest, client, work, env):
    print("==> Resolving and downloading dependencies from crates.io")
    info = metadata(args, manifest, work, env)
    lock = Path(info["workspace_root"]) / "Cargo.lock"
    command = [args.cargo, "fetch", "--manifest-path", str(manifest)]
    if lock.is_file():
        command.append("--locked")
    else:
        print(f"    Cargo will create: {lock}")
    run_command(command, args, work, env)
    packages = tomllib.loads(lock.read_text()).get("package", [])
    selected = []
    allowed = {f"sparse+{client.url(repo)}" for repo in (args.internal_repo, args.virtual_repo, args.external_repo)}
    for package in packages:
        source = package.get("source")
        if source == CRATES_IO_SOURCE or source == "sparse+https://index.crates.io/":
            selected.append(package)
        elif source is None:
            continue
        elif source in allowed:
            print(f"    Already an Artifact Keeper dependency: {package['name']}@{package['version']}")
        else:
            raise RuntimeError(f"Cannot mirror dependency source {source}; Git and third-party registries need separate handling")
    print(f"    Resolved external crates: {len(selected)}")
    uploaded = skipped = failed = 0
    for package in sorted(selected, key=lambda item: (item["name"], item["version"])):
        name, version = package["name"], package["version"]
        print("-" * 60)
        print(f"Dependency: {name}@{version}")
        print(f"Artifact: {name}-{version}.crate")
        try:
            entry = upstream_entry(client, name, version)
            if package.get("checksum") != entry["cksum"]:
                raise RuntimeError("Cargo.lock checksum differs from the upstream index")
            existing = client.entry(args.external_repo, name, version)
            if existing:
                if existing["cksum"] != entry["cksum"]:
                    raise RuntimeError("Existing crate has different bytes; refusing to treat it as an upstream mirror")
                print(f"WARNING: {name}@{version} is already published; skipping.")
                skipped += 1
                continue
            archives = list((Path(env["CARGO_HOME"]) / "registry/cache").glob(f"*/{name}-{version}.crate"))
            matches = [path for path in archives if sha256(path.read_bytes()) == entry["cksum"]]
            if not matches:
                raise RuntimeError("Cargo cache has no original .crate matching the upstream checksum")
            crate = matches[0].read_bytes()
            print(f"MISSING: {name}@{version}")
            print("Uploading to Artifact Keeper...")
            try:
                client.request(client.url(args.external_repo) + "api/v1/crates/new", data=publish_payload(entry, crate))
            except urllib.error.HTTPError as exc:
                exc.close()
                if exc.code != 409:
                    raise
                existing = client.entry(args.external_repo, name, version)
                if not existing or existing["cksum"] != entry["cksum"]:
                    raise RuntimeError("Concurrent publication conflicts with upstream crate") from exc
                print(f"WARNING: {name}@{version} was published concurrently; skipping.")
                skipped += 1
                continue
            stored = client.entry(args.external_repo, name, version)
            if not stored or stored["cksum"] != entry["cksum"]:
                raise RuntimeError("Published crate is missing from the index or has a different checksum")
            downloaded = client.request(client.url(args.external_repo) + f"api/v1/crates/{name}/{version}/download")
            if sha256(downloaded) != entry["cksum"]:
                raise RuntimeError("Downloaded mirror bytes do not match crates.io")
            print(f"UPLOADED: {name}@{version}")
            uploaded += 1
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            print(f"ERROR: {exc}", file=sys.stderr)
            failed += 1
        print()
    return uploaded, skipped, failed


def publish_project(args, manifest, client, work, env):
    info = metadata(args, manifest, work, env)
    packages = [package for package in info["packages"] if package["name"] == args.package] if args.package else [package for package in info["packages"] if Path(package["manifest_path"]).resolve() == manifest]
    if len(packages) != 1:
        raise RuntimeError("Select one crate with --package when publishing a virtual workspace")
    package = packages[0]
    allowed = package.get("publish")
    if allowed is not None and "cargo-internal" not in allowed:
        raise RuntimeError("Cargo.toml package.publish does not allow cargo-internal")
    name, version = package["name"], package["version"]
    print("-" * 60)
    print(f"Project: {name}@{version}")
    print(f"Artifact: {name}-{version}.crate")
    if client.entry(args.internal_repo, name, version):
        print(f"WARNING: {name}@{version} is already published; skipping.")
        return 0, 1, 0
    command = [args.cargo, "publish", "--registry", "cargo-internal", "--manifest-path", str(manifest), "--package", name]
    lock = Path(info["workspace_root"]) / "Cargo.lock"
    if lock.is_file():
        command.append("--locked")
    if args.allow_dirty:
        command.append("--allow-dirty")
    print(f"MISSING: {name}@{version}")
    print("Uploading to Artifact Keeper...")
    try:
        run_command(command, args, work, env, capture_output=False)
    except RuntimeError:
        # Cargo can fail its post-upload index poll or race another publisher.
        if client.entry(args.internal_repo, name, version):
            print(f"WARNING: {name}@{version} now exists in the registry; skipping.")
            return 0, 1, 0
        raise
    if not client.entry(args.internal_repo, name, version):
        raise RuntimeError("Cargo publish completed but the version is missing from the index")
    print(f"UPLOADED: {name}@{version}")
    return 1, 0, 0


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
            raise RuntimeError("ARTIFACT_KEEPER_TOKEN is not set")
        executable = shutil.which(args.cargo)
        if not executable:
            raise RuntimeError("Cargo is not installed; install Rust or supply --cargo /path/to/cargo")
        args.cargo = executable
        if not args.ca_bundle.is_file():
            raise RuntimeError(f"CA bundle does not exist: {args.ca_bundle}")
        client = RegistryClient(args, token)
        destination = args.internal_repo if args.project_dir else args.external_repo
        client.validate_repository(destination, "local")
        if args.project_dir:
            client.validate_repository(args.virtual_repo, "virtual")
        args.work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="run-", dir=args.work_dir) as directory:
            work = Path(directory)
            for parent in work.parents:
                if any((parent / ".cargo" / name).is_file() for name in ("config", "config.toml")):
                    raise RuntimeError("--work-dir must be outside directories with ancestor .cargo configuration")
            manifest = prepare_manifest(args, work)
            env = cargo_environment(args, work, token, bool(args.project_dir))
            print(f"==> Mode: {'internal project' if args.project_dir else 'external dependencies'}")
            print(f"    Manifest: {manifest}")
            print(f"    Repository: {client.url(destination)}")
            if args.project_dir:
                result = publish_project(args, manifest, client, work, env)
            else:
                result = mirror_dependencies(args, manifest, client, work, env)
        print_summary(*result)
        return 1 if result[2] else 0
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        print(f"ERROR: {exc}", file=sys.stderr)
        print_summary(0, 0, 1)
        return 1


if __name__ == "__main__":
    sys.exit(main())
