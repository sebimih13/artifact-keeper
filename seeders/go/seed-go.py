#!/usr/bin/env python3

"""
Seed an Artifact Keeper Go repository using Python 3.9+ and the Go toolchain.
No third-party Python modules, Makefile, curl or jq are required.

Configuration:
    export ARTIFACT_KEEPER_TOKEN=...
    export AK_API=https://localhost/go/go

Choose exactly one input:
    python3 seed-go.py --go-mod /path/to/go.mod
    python3 seed-go.py --dependency github.com/google/uuid@v1.6.0
    python3 seed-go.py --project-dir /path/to/module --version v1.0.0

--go-mod mirrors the dependency graph without publishing the root module.
--dependency resolves a module version/query and mirrors its dependency graph.
--project-dir mirrors dependencies and publishes the current directory's module.
It requires an explicit release version matching the go.mod module path; build
and test your project first. The source directory is not modified. Archives
include eligible working-tree files (including untracked files), not just Git
tracked files. Go's official ZIP builder excludes VCS directories, vendor,
nested modules and symlinks and enforces module filename/size constraints.
The embedded temporary Go helper uses pinned golang.org/x/mod v0.28.0 and
requires Go 1.24+ plus access to proxy.golang.org on its first run.

Use --ca-bundle with a PEM bundle containing public roots and the local CA.
GO selects the Go executable; --goproxy controls dependency downloads.
Temporary module/build caches keep the shared Go cache untouched. go.sum is
copied when available and checksum verification remains enabled by default.
GOWORK is disabled; local replacements must be published and changed to
versioned requirements/replacements before seeding. Versioned replacements
are supported for dependency mirroring, but not in a published root module
because consumers do not inherit its replace/exclude directives.

Artifact Keeper accepts .mod and .zip PUTs and generates .info/list/@latest.
Original upstream .info timestamps cannot be preserved through this endpoint.
The summary counts individual .mod/.zip files; existing bytes must match.
Partial uploads can be repaired by rerunning; the two PUTs are not atomic.

Install library packages with go get <module-or-package>@<version> and command
packages with go install <module>/cmd/<tool>@<version>. Set GOPROXY to the local
Go URL and configure client authentication/CA trust. For internal modules set
GONOSUMDB to their module prefix; keep public checksum verification enabled.
If GOPRIVATE matches these modules, set GONOPROXY=none to use the proxy.
A GOPROXY ending in ,direct allows fallback to external version control.

go.sum is a checksum history, not a lockfile: go.mod determines the dependency
graph. go.work, local replacements, vendored/GOPATH projects, non-Go build
assets, private upstream credentials and downloaded toolchains require
separate handling. Module archives cover platforms/build tags as source;
this does not build or test every platform. A single --dependency argument
must identify a module, not merely a package subdirectory within a module.
"""

import argparse
import hashlib
import json
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

from pathlib import Path


DEFAULT_GOPROXY = "https://proxy.golang.org,direct"
DEFAULT_WORK_DIR = "/tmp/artifact-keeper-go-seed"
DEFAULT_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
HTTP_TIMEOUT = 30
COMMAND_TIMEOUT = 1800


# Use the official module archive implementation instead of duplicating Go's
# module path, semantic import versioning and ZIP rules in Python.
GO_ARCHIVE_HELPER = r'''
package main

import (
    "encoding/json"
    "fmt"
    "os"
    "path/filepath"
    "strings"

    "golang.org/x/mod/modfile"
    "golang.org/x/mod/module"
    "golang.org/x/mod/semver"
    "golang.org/x/mod/zip"
)

func run() error {
    dir, version, output := os.Args[1], os.Args[2], os.Args[3]
    data, err := os.ReadFile(filepath.Join(dir, "go.mod"))
    if err != nil { return err }
    mod, err := modfile.Parse("go.mod", data, nil)
    if err != nil { return err }
    if mod.Module == nil { return fmt.Errorf("go.mod has no module directive") }
    path := mod.Module.Mod.Path
    if semver.Canonical(version) != version || strings.Contains(version, "+") {
        return fmt.Errorf("use a canonical release or prerelease version, such as v1.0.0")
    }
    if module.IsPseudoVersion(version) {
        return fmt.Errorf("project publishing requires a release version, not a fabricated pseudo-version")
    }
    if err := module.Check(path, version); err != nil { return err }
    if len(mod.Replace) != 0 || len(mod.Exclude) != 0 {
        return fmt.Errorf("published modules must not depend on replace/exclude directives; consumers do not inherit them")
    }
    file, err := os.Create(output)
    if err != nil { return err }
    err = zip.CreateFromDir(file, module.Version{Path: path, Version: version}, dir)
    closeErr := file.Close()
    if err != nil { return err }
    if closeErr != nil { return closeErr }
    return json.NewEncoder(os.Stdout).Encode(map[string]string{
        "Path": path, "Version": version, "Zip": output,
        "GoMod": filepath.Join(dir, "go.mod"),
    })
}

func main() {
    if err := run(); err != nil {
        fmt.Fprintln(os.Stderr, err)
        os.Exit(1)
    }
}
'''


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        raise RuntimeError("Unexpected repository redirect; use the final Go repository URL")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download Go modules and publish missing artifacts to Artifact Keeper."
    )

    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--go-mod", type=Path, help="go.mod whose dependencies should be mirrored")
    inputs.add_argument("--dependency", help="Module@version or query, for example github.com/google/uuid@v1.6.0")
    inputs.add_argument("--project-dir", type=Path, help="Internal module directory to publish, including dependencies")

    parser.add_argument("--version", help="Release version for --project-dir, for example v1.0.0")
    parser.add_argument("--artifact-keeper", default=os.environ.get("AK_API"), help="Go repository URL (default: AK_API)")
    parser.add_argument("--goproxy", default=os.environ.get("GOPROXY", DEFAULT_GOPROXY))
    parser.add_argument("--work-dir", type=Path, default=Path(DEFAULT_WORK_DIR), help="Parent of temporary workspaces; existing files are preserved")
    parser.add_argument("--ca-bundle", type=Path, default=Path(DEFAULT_CA_BUNDLE))
    parser.add_argument("--http-timeout", type=int, default=HTTP_TIMEOUT)
    parser.add_argument("--command-timeout", type=int, default=COMMAND_TIMEOUT)

    args = parser.parse_args()

    if not args.artifact_keeper:
        parser.error("--artifact-keeper or AK_API is required")

    url = urllib.parse.urlsplit(args.artifact_keeper)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or not re.fullmatch(r"/go/[^/]+/?", url.path)
        or any(character.isspace() for character in args.artifact_keeper)
    ):
        parser.error("Use a Go repository URL such as https://localhost/go/go, without credentials; /npm/npm is not a Go endpoint")

    if bool(args.project_dir) != bool(args.version):
        parser.error("--project-dir and --version must be supplied together")
    if args.dependency and (
        "@" not in args.dependency
        or args.dependency.startswith("-")
        or any(character.isspace() for character in args.dependency)
    ):
        parser.error("--dependency must be a module@version or module@query")
    if args.dependency == "":
        parser.error("--dependency must not be empty")
    if args.http_timeout <= 0 or args.command_timeout <= 0:
        parser.error("Timeouts must be positive")

    for name in ("go_mod", "project_dir", "work_dir", "ca_bundle"):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.expanduser().resolve())

    if args.go_mod and not args.go_mod.is_file():
        parser.error(f"go.mod does not exist: {args.go_mod}")
    if args.project_dir and not (args.project_dir / "go.mod").is_file():
        parser.error(f"PROJECT_DIR must contain go.mod: {args.project_dir}")
    if not args.ca_bundle.is_file():
        parser.error(f"CA bundle does not exist: {args.ca_bundle}")
    if args.project_dir and args.work_dir.is_relative_to(args.project_dir):
        parser.error("--work-dir must be outside --project-dir so temporary files are not archived")

    args.artifact_keeper = args.artifact_keeper.rstrip("/") + "/"
    return args


def run_command(command, *, cwd, env, timeout=COMMAND_TIMEOUT):
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            timeout=timeout,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout

    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(map(str, command))}") from exc

    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Command failed:\n  {' '.join(map(str, command))}\n"
            f"{exc.stdout or ''}{exc.stderr or ''}"
        ) from exc


def go_environment(args, work_dir: Path):
    env = os.environ.copy()
    env.pop("ARTIFACT_KEEPER_TOKEN", None)
    env["GOENV"] = "off"
    env["GOFLAGS"] = ""
    env["GOWORK"] = "off"
    env["GO111MODULE"] = "on"
    env["GOTOOLCHAIN"] = "local"
    env["GOPROXY"] = args.goproxy
    env["GOMODCACHE"] = str(work_dir / "module-cache")
    env["GOCACHE"] = str(work_dir / "build-cache")
    env["GOPATH"] = str(work_dir / "gopath")
    env["SSL_CERT_FILE"] = str(args.ca_bundle)
    return env


def parse_json_stream(output: str):
    decoder = json.JSONDecoder()
    records = []
    position = 0

    while position < len(output):
        if output[position].isspace():
            position += 1
            continue
        record, position = decoder.raw_decode(output, position)
        if not isinstance(record, dict):
            raise RuntimeError("Expected Go module JSON objects")
        if record.get("Error"):
            raise RuntimeError(f"{record.get('Path', 'Module')}: {record['Error']}")
        records.append(record)

    return records


def prepare_manifest(go: str, source: Path, directory: Path, env, timeout: int):
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, directory / "go.mod")
    if source.with_name("go.sum").is_file():
        shutil.copyfile(source.with_name("go.sum"), directory / "go.sum")

    manifest = json.loads(run_command(
        [go, "mod", "edit", "-json"], cwd=directory, env=env, timeout=timeout,
    ))
    for replacement in manifest.get("Replace") or []:
        if not replacement["New"].get("Version"):
            raise RuntimeError(
                f"Local replacement {replacement['Old']['Path']} => {replacement['New']['Path']} "
                "cannot be mirrored; publish it separately and use a versioned dependency"
            )


def download_dependencies(args, go: str, work_dir: Path, env):
    directory = work_dir / "resolution"
    directory.mkdir()
    records = []

    if args.dependency:
        print("==> Mode: single dependency")
        print(f"    Dependency: {args.dependency}")
        (directory / "go.mod").write_text("module example.com/artifact-keeper-seed\n\ngo 1.24.0\n", encoding="utf-8")
        records = parse_json_stream(run_command(
            [go, "mod", "download", "-json", args.dependency],
            cwd=directory, env=env, timeout=args.command_timeout,
        ))
        if len(records) != 1 or not records[0].get("GoMod"):
            raise RuntimeError("Expected one downloadable module; use its module path, not a package subdirectory")
        prepare_manifest(go, Path(records[0]["GoMod"]), directory, env, args.command_timeout)

    else:
        print("==> Mode: Go project" if args.project_dir else "==> Mode: dependencies only")
        source = args.project_dir / "go.mod" if args.project_dir else args.go_mod
        print(f"    Manifest: {source}")
        prepare_manifest(go, source, directory, env, args.command_timeout)

    print()
    print("==> Downloading dependencies from Internet")
    print(f"    GOPROXY: {args.goproxy}")
    records.extend(parse_json_stream(run_command(
        [go, "mod", "download", "-json", "all"],
        cwd=directory, env=env, timeout=args.command_timeout,
    )))

    modules = {}
    for record in records:
        if not all(record.get(key) for key in ("Path", "Version", "GoMod", "Zip")):
            raise RuntimeError(f"Incomplete downloaded module: {record.get('Path', 'unknown')}")
        for key in ("GoMod", "Zip"):
            path = Path(record[key]).resolve()
            if not path.is_relative_to(Path(env["GOMODCACHE"])) or not path.is_file():
                raise RuntimeError(f"Unexpected Go cache artifact: {path}")
        modules[(record["Path"], record["Version"])] = record

    return [modules[key] for key in sorted(modules)]


def build_project(args, go: str, work_dir: Path, env):
    print("==> Building project module archive")
    print(f"    Project: {args.project_dir}")
    print(f"    Version: {args.version}")
    helper = work_dir / "archive-helper"
    helper.mkdir()
    (helper / "go.mod").write_text(
        "module example.com/artifact-keeper-archive-helper\n\ngo 1.24.0\n\nrequire golang.org/x/mod v0.28.0\n",
        encoding="utf-8",
    )
    (helper / "main.go").write_text(GO_ARCHIVE_HELPER, encoding="utf-8")
    helper_env = env.copy()
    helper_env["GOPROXY"] = DEFAULT_GOPROXY
    output = run_command(
        [go, "run", "-mod=mod", ".", str(args.project_dir), args.version, str(work_dir / "project.zip")],
        cwd=helper, env=helper_env, timeout=args.command_timeout,
    )
    record = json.loads(output)
    # The sidecar must be the exact go.mod included in the archive.
    with zipfile.ZipFile(record["Zip"]) as archive:
        manifest = archive.read(f"{record['Path']}@{record['Version']}/go.mod")
    (work_dir / "project.mod").write_bytes(manifest)
    record["GoMod"] = str(work_dir / "project.mod")
    return record


def escape_module(value: str):
    return "".join("!" + character.lower() if "A" <= character <= "Z" else character for character in value)


def artifact_url(registry: str, module, extension: str):
    path = escape_module(module["Path"]) + "/@v/" + escape_module(module["Version"]) + extension
    return registry + urllib.parse.quote(path, safe="/!@")


def file_digest(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def artifact_matches(opener, url: str, token: str, path: Path, timeout: int):
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with opener.open(request, timeout=timeout) as response:
            digest = hashlib.sha256()
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.digest() != file_digest(path):
            raise RuntimeError(f"Existing artifact has different bytes: {url}; choose a new project version")
        return True

    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 404:
            return False
        raise RuntimeError(f"Artifact Keeper returned HTTP {exc.code} for {url}") from exc


def upload_file(opener, url: str, token: str, path: Path, content_type: str, timeout: int):
    try:
        with path.open("rb") as stream:
            request = urllib.request.Request(
                url, data=stream, method="PUT",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": content_type,
                    "Content-Length": str(path.stat().st_size),
                },
            )
            with opener.open(request, timeout=timeout) as response:
                if response.status not in (200, 201, 202, 204):
                    raise RuntimeError(f"Unexpected upload status: HTTP {response.status}")
        return True

    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 409 and artifact_matches(opener, url, token, path, timeout):
            return False
        raise RuntimeError(f"Upload failed with HTTP {exc.code}: {url}") from exc


def upload_artifacts(args, modules, token: str, label: str):
    context = ssl.create_default_context(cafile=str(args.ca_bundle))
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
    uploaded = skipped = failed = 0

    for module in modules:
        print("-" * 60)
        print(f"{label}: {module['Path']}@{module['Version']}")
        artifacts = [
            (".mod", Path(module["GoMod"]), "text/plain"),
            (".zip", Path(module["Zip"]), "application/zip"),
        ]

        try:
            # Check both siblings before writing either, including partial uploads.
            existing = [
                artifact_matches(opener, artifact_url(args.artifact_keeper, module, suffix), token, path, args.http_timeout)
                for suffix, path, _ in artifacts
            ]

        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            failed += len(artifacts)
            continue

        for (suffix, path, content_type), exists in zip(artifacts, existing):
            filename = module["Version"] + suffix
            print(f"Artifact: {filename}")
            if exists:
                print(f"SKIP: {filename} already exists in Artifact Keeper.")
                skipped += 1
                continue
            print(f"MISSING: {filename}")
            print("Uploading to Artifact Keeper...")
            try:
                created = upload_file(
                    opener, artifact_url(args.artifact_keeper, module, suffix), token,
                    path, content_type, args.http_timeout,
                )
                if created:
                    print(f"UPLOADED: {filename}")
                    uploaded += 1
                else:
                    print(f"SKIP: {filename} already exists in Artifact Keeper.")
                    skipped += 1
            except Exception as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                failed += 1
        print()

    return uploaded, skipped, failed


def seed(args, go: str, token: str, work_dir: Path):
    env = go_environment(args, work_dir)
    project = build_project(args, go, work_dir, env) if args.project_dir else None
    dependencies = download_dependencies(args, go, work_dir, env)
    print()
    print("==> Uploading dependencies to Artifact Keeper")
    print(f"    Repository: {args.artifact_keeper}")
    if not dependencies:
        print("    No dependency artifacts to upload")
    uploaded, skipped, failed = upload_artifacts(args, dependencies, token, "Dependency")

    if project and not failed:
        print()
        print("==> Publishing project artifacts")
        counts = upload_artifacts(args, [project], token, "Project")
        uploaded += counts[0]
        skipped += counts[1]
        failed += counts[2]
    elif project:
        print("ERROR: Project publishing withheld because dependency uploads failed", file=sys.stderr)

    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"Total:    {uploaded + skipped + failed}")
    print(f"Uploaded: {uploaded}")
    print(f"Skipped:  {skipped}")
    print(f"Failed:   {failed}")
    return 1 if failed else 0


def main():
    args = parse_args()
    token = os.environ.get("ARTIFACT_KEEPER_TOKEN")
    if not token:
        print("ERROR: ARTIFACT_KEEPER_TOKEN is not set", file=sys.stderr)
        return 1
    go = shutil.which(os.environ.get("GO", "go"))
    if not go:
        print("ERROR: Go executable was not found", file=sys.stderr)
        return 1

    try:
        args.work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="run-", dir=args.work_dir) as directory:
            return seed(args, go, token, Path(directory))

    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
