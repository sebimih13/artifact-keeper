#!/usr/bin/env python3
"""Download and publish Go modules to an Artifact Keeper Go repository.

Examples:
  python3 scripts/upload-go.py \
      github.com/git-ecosystem/trace2receiver@v0.5.6

  python3 scripts/upload-go.py --repository go --token-file /tmp/ak-token \
      module.example/project@v1.2.3 another.example/module@v0.4.0

Without --token-file, the script prompts for the selected user's password.
The target repository is created as a public local Go repository if missing.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
import zipfile


def parse_json_stream(raw: str) -> list[dict]:
    decoder = json.JSONDecoder()
    position = 0
    results = []
    while position < len(raw):
        while position < len(raw) and raw[position].isspace():
            position += 1
        if position >= len(raw):
            break
        value, position = decoder.raw_decode(raw, position)
        results.append(value)
    return results


def split_module_spec(spec: str) -> tuple[str, str]:
    if "@" not in spec:
        raise ValueError(f"Module must include an exact version: {spec}@v1.2.3")
    module, version = spec.rsplit("@", 1)
    if not module or not version:
        raise ValueError(f"Invalid module specification: {spec}")
    return module, version


def escape_go_path(value: str) -> str:
    """Apply the uppercase escaping required by the GOPROXY protocol."""
    escaped = []
    for character in value:
        if "A" <= character <= "Z":
            escaped.extend(("!", character.lower()))
        else:
            escaped.append(character)
    return "".join(escaped)


class ApiError(RuntimeError):
    def __init__(self, method: str, path: str, error: HTTPError):
        self.status = error.code
        body = error.read().decode(errors="replace")
        super().__init__(f"{method} {path}: HTTP {error.code}: {body}")


class ArtifactKeeperApi:
    def __init__(self, base_url: str, token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        data: bytes | dict | None = None,
        content_type: str | None = None,
    ) -> tuple[int, bytes]:
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        request = Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=180) as response:
                return response.status, response.read()
        except HTTPError as error:
            raise ApiError(method, path, error) from None

    def json(self, method: str, path: str, data: dict | None = None) -> dict:
        _, body = self.request(method, path, data)
        return json.loads(body) if body else {}


def authenticate(api: ArtifactKeeperApi, args: argparse.Namespace) -> None:
    if args.token_file:
        api.token = args.token_file.read_text().strip()
        if not api.token:
            raise ValueError(f"Token file is empty: {args.token_file}")
        return

    password = os.environ.get(args.password_env) if args.password_env else None
    if password is None:
        password = getpass.getpass(f"Artifact Keeper password for {args.username}: ")
    response = api.json(
        "POST",
        "/api/v1/auth/login",
        {"username": args.username, "password": password},
    )
    api.token = response["access_token"]


def ensure_repository(api: ArtifactKeeperApi, args: argparse.Namespace) -> None:
    path = "/api/v1/repositories/" + quote(args.repository, safe="")
    try:
        repository = api.json("GET", path)
    except ApiError as error:
        if error.status != 404 or args.no_create_repository:
            raise
        repository = api.json(
            "POST",
            "/api/v1/repositories",
            {
                "key": args.repository,
                "name": args.repository_name,
                "description": "Local Go module dependencies",
                "format": "go",
                "repo_type": "local",
                "is_public": not args.private,
            },
        )
        print(f"Created local Go repository: {args.repository}")

    if repository.get("format") != "go" or repository.get("repo_type") != "local":
        raise ValueError(
            f"Repository {args.repository!r} must be a local Go repository"
        )


def download_modules(specs: list[str], work: Path) -> list[dict]:
    environment = os.environ.copy()
    environment["GOMODCACHE"] = str(work / "module-cache")
    environment["GOPATH"] = str(work / "gopath")
    command = ["go", "mod", "download", "-json", *specs]
    result = subprocess.run(
        command,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    modules = parse_json_stream(result.stdout)
    if len(modules) != len(specs):
        raise RuntimeError(
            f"Go returned {len(modules)} modules for {len(specs)} requests"
        )
    return modules


def validate_module(module: dict, requested: tuple[str, str]) -> None:
    expected_path, expected_version = requested
    if module.get("Error"):
        raise RuntimeError(module["Error"])
    if (module.get("Path"), module.get("Version")) != requested:
        raise RuntimeError(
            f"Resolved {module.get('Path')}@{module.get('Version')}, "
            f"expected {expected_path}@{expected_version}"
        )

    go_mod = Path(module["GoMod"]).read_text()
    declaration = next(
        (line.split(None, 1)[1] for line in go_mod.splitlines() if line.startswith("module ")),
        None,
    )
    if declaration != expected_path:
        raise RuntimeError(
            f"go.mod declares {declaration!r}, expected {expected_path!r}"
        )

    expected_prefix = f"{expected_path}@{expected_version}/"
    with zipfile.ZipFile(module["Zip"]) as archive:
        names = archive.namelist()
    if not names or any(not name.startswith(expected_prefix) for name in names):
        raise RuntimeError(f"Invalid canonical module ZIP layout for {expected_prefix}")


def proxy_path(repository: str, module: str, version: str, suffix: str) -> str:
    escaped_module = escape_go_path(module)
    escaped_version = escape_go_path(version)
    encoded = quote(f"{escaped_module}/@v/{escaped_version}{suffix}", safe="/!@")
    return f"/go/{quote(repository, safe='')}/{encoded}"


def publish_file(
    api: ArtifactKeeperApi,
    path: str,
    source: Path,
    content_type: str,
) -> None:
    content = source.read_bytes()
    try:
        status, _ = api.request("PUT", path, content, content_type)
        if status != 201:
            raise RuntimeError(f"Unexpected upload status {status} for {path}")
    except ApiError as error:
        if error.status != 409:
            raise
        _, hosted = api.request("GET", path)
        if hosted != content:
            raise RuntimeError(f"Existing hosted file differs from source: {path}")
        print(f"Already present: {path}")
        return

    _, hosted = api.request("GET", path)
    if hashlib.sha256(hosted).digest() != hashlib.sha256(content).digest():
        raise RuntimeError(f"Hosted checksum differs after upload: {path}")
    print(f"Published: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("modules", nargs="+", help="Exact module@version coordinates")
    parser.add_argument("--repository", default="go", help="Target repository key")
    parser.add_argument("--repository-name", default="Go Modules")
    parser.add_argument("--url", default="http://localhost")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--username", default="admin")
    parser.add_argument(
        "--password-env",
        metavar="NAME",
        help="Read the login password from this environment variable",
    )
    parser.add_argument(
        "--no-create-repository",
        action="store_true",
        help="Fail instead of creating a missing target repository",
    )
    parser.add_argument(
        "--private",
        action="store_true",
        help="Make a newly created repository private",
    )
    args = parser.parse_args()

    requested = [split_module_spec(spec) for spec in args.modules]
    if len(set(requested)) != len(requested):
        parser.error("Duplicate module coordinates were supplied")

    api = ArtifactKeeperApi(args.url)
    authenticate(api, args)
    ensure_repository(api, args)

    with tempfile.TemporaryDirectory(prefix="artifact-keeper-go-") as directory:
        modules = download_modules(args.modules, Path(directory))
        for module, coordinate in zip(modules, requested, strict=True):
            validate_module(module, coordinate)
            name, version = coordinate
            publish_file(
                api,
                proxy_path(args.repository, name, version, ".mod"),
                Path(module["GoMod"]),
                "text/plain",
            )
            publish_file(
                api,
                proxy_path(args.repository, name, version, ".zip"),
                Path(module["Zip"]),
                "application/zip",
            )

    print(f"Verified {len(requested)} module version(s).")
    print(f"GOPROXY={args.url.rstrip('/')}/go/{args.repository}")


if __name__ == "__main__":
    try:
        main()
    except (ApiError, OSError, RuntimeError, ValueError, zipfile.BadZipFile) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
