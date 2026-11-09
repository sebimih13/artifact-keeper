#!/usr/bin/env python3
"""Upload existing .deb files to an Artifact Keeper local Debian repository.

Python 3.10+, standard library only. gzip/xz/bzip2/plain control archives are
read without extracting or executing package contents. Zstd control archives
require dpkg-deb on PATH. See README.md for signing and consumer configuration.
"""

import argparse
import hashlib
import io
import json
import os
import re
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def control_fields(text):
    fields = {}
    for line in text.splitlines():
        if not line or line[0].isspace():
            continue
        key, separator, value = line.partition(":")
        if not separator or key in fields:
            raise ValueError("Malformed or duplicate Debian control field")
        fields[key] = value.strip()
    for name, pattern in (("Package", r"[a-z0-9][a-z0-9+.-]+"),
                          ("Version", r"[0-9][A-Za-z0-9.+:~\-]*"),
                          ("Architecture", r"[a-z0-9][a-z0-9-]*")):
        if not re.fullmatch(pattern, fields.get(name, "")):
            raise ValueError(f"Missing/invalid control field: {name}")
    return fields


def inspect_deb(path):
    """Return the upload bytes and identity; never trust the input filename."""
    data = path.read_bytes()
    if data[:8] != b"!<arch>\n":
        raise ValueError("Not a Debian ar archive")
    offset, members = 8, {}
    while offset < len(data):
        header = data[offset:offset + 60]
        if len(header) != 60 or header[58:] != b"`\n":
            raise ValueError("Malformed Debian ar header")
        size = int(header[48:58].strip())
        name = header[:16].decode("ascii").strip().rstrip("/")
        offset += 60
        if size < 0 or offset + size > len(data) or name in members:
            raise ValueError("Invalid Debian ar member")
        members[name] = data[offset:offset + size]
        offset += size + size % 2
    if members.get("debian-binary") != b"2.0\n":
        raise ValueError("Expected Debian binary format 2.0")
    controls = [name for name in members if name == "control.tar" or name.startswith("control.tar.")]
    if len(controls) != 1 or not any(name == "data.tar" or name.startswith("data.tar.") for name in members):
        raise ValueError("Missing or ambiguous control/data archive")
    name = controls[0]
    if name.endswith(".zst"):
        # Let Debian's own tool handle Zstandard, without unpacking/installing.
        text = subprocess.run(["dpkg-deb", "--field", str(path.resolve())],
                              check=True, capture_output=True, text=True, timeout=60).stdout
    else:
        with tarfile.open(fileobj=io.BytesIO(members[name]), mode="r:*") as archive:
            entries = [e for e in archive.getmembers() if e.name in ("control", "./control")]
            if len(entries) != 1 or not entries[0].isfile() or entries[0].size > 1024 * 1024:
                raise ValueError("Expected one regular control file smaller than 1 MiB")
            text = archive.extractfile(entries[0]).read().decode("utf-8")
    return data, control_fields(text)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        fp.close()
        raise RuntimeError("Server redirected a request; configure its final base URL")


class Client:
    def __init__(self, base, token, ca_bundle=None, timeout=60):
        self.base, self.token, self.timeout = base.rstrip("/"), token, timeout
        context = ssl.create_default_context()
        if ca_bundle:
            context.load_verify_locations(cafile=str(ca_bundle))
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))

    def request(self, path, method="GET", data=None, content_type=None):
        headers = {"Authorization": f"Bearer {self.token}", "User-Agent": "artifact-keeper-debian-seeder/1.0"}
        if content_type:
            headers["Content-Type"] = content_type
        for attempt in range(5):
            try:
                request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
                with self.opener.open(request, timeout=self.timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or attempt == 4:
                    raise
                retry = exc.headers.get("Retry-After", "1")
                exc.close()
                delay = int(retry) if retry.isdigit() else 1
                if delay > 60:
                    raise RuntimeError("Rate limit wait exceeds 60s; rerun later") from exc
                time.sleep(max(1, delay))

    def json(self, path, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        return json.loads(self.request(path, method, data, "application/json"))

    def existing(self, path):
        try:
            return self.request(path)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 404:
                return None
            raise


def ensure_repository(client, key, create=False, private=False):
    path = f"/api/v1/repositories/{key}"
    raw = client.existing(path)
    if raw is None:
        if not create:
            raise ValueError(f"Repository {key} is missing; use --create-repo with an administrator token")
        repo = client.json("/api/v1/repositories", "POST", {
            "key": key, "name": key, "format": "debian", "repo_type": "local",
            "is_public": not private,
            "description": "Local Debian/Ubuntu development tools for amd64 and arm64",
        })
        print(f"Created repository: {key}")
    else:
        repo = json.loads(raw)
    if repo.get("format") != "debian" or repo.get("repo_type") != "local":
        raise ValueError(f"{key} must be a local Debian repository")
    signing_path = f"/api/v1/signing/repositories/{repo['id']}/config"
    config = client.json(signing_path)
    signing_key = config.get("key")
    if not (config.get("sign_metadata") and signing_key and signing_key.get("key_type") == "gpg" and signing_key.get("is_active")):
        if not create:
            raise ValueError("Repository needs active GPG metadata signing; ask an administrator to run --create-repo")
        if config.get("signing_key_id"):
            raise ValueError("Existing signing configuration needs administrator review; refusing to replace its key")
        signing_key = client.json("/api/v1/signing/keys", "POST", {
            "name": f"{key}-apt", "key_type": "gpg", "algorithm": "rsa4096", "repository_id": repo["id"],
        })
        client.json(signing_path, "POST", {"signing_key_id": signing_key["id"], "sign_metadata": True})
        print("Configured GPG metadata signing")
    print(f"Signing fingerprint: {signing_key.get('fingerprint')}")
    return repo, signing_key


def upload(client, repo, component, path):
    data, fields = inspect_deb(path)
    package, version, arch = (fields[k] for k in ("Package", "Version", "Architecture"))
    filename = f"{package}_{version}_{arch}.deb"
    prefix = package[:4] if package.startswith("lib") else package[0]
    target = f"/debian/{repo}/pool/{component}/{prefix}/{package}/{urllib.parse.quote(filename, safe='')}"
    digest = hashlib.sha256(data).hexdigest()

    def matches(existing):
        if hashlib.sha256(existing).hexdigest() != digest:
            raise ValueError(f"Conflicting bytes for {filename}; publish a NEW version")

    existing = client.existing(target)
    if existing is not None:
        matches(existing)
        print(f"WARNING: already published, skipped {filename}")
        return "skipped"
    try:
        client.request(target, "PUT", data, "application/vnd.debian.binary-package")
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code != 409:
            raise
        existing = client.existing(target)
        if existing is None:
            raise ValueError(f"Conflict but {filename} cannot be read back") from exc
        matches(existing)
        print(f"WARNING: concurrent publication, skipped {filename}")
        return "skipped"
    matches(client.request(target))
    print(f"Uploaded and verified: {filename}")
    return "uploaded"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--deb", type=Path, action="append", help="Existing .deb; repeat for multiple files")
    inputs.add_argument("--directory", type=Path, help="Upload all .deb files recursively")
    parser.add_argument("--artifact-keeper", default=os.environ.get("AK_API", "https://localhost"))
    parser.add_argument("--repo", default="apt-local-devtools")
    parser.add_argument("--component", default="main")
    parser.add_argument("--create-repo", action="store_true", help="Create repository and configure signing (administrator)")
    parser.add_argument("--private", action="store_true", help="New repository requires read authentication")
    parser.add_argument("--ca-bundle", type=Path, help="Additional PEM CA roots; keeps system trust")
    parser.add_argument("--export-key", type=Path, help="Save public signing key for consumer Signed-By")
    parser.add_argument("--http-timeout", type=int, default=60)
    args = parser.parse_args()
    url = urllib.parse.urlsplit(args.artifact_keeper)
    if (url.scheme not in ("http", "https") or not url.hostname or url.username is not None
            or url.password is not None or url.path not in ("", "/") or url.query or url.fragment
            or any(c.isspace() for c in args.artifact_keeper)):
        parser.error("Use a server base URL, e.g. https://localhost (no /debian or /api/v1 suffix)")
    for value in (args.repo, args.component):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value):
            parser.error("Repository/component must contain lowercase letters, digits, hyphens or underscores")
    if args.http_timeout <= 0:
        parser.error("--http-timeout must be positive")
    if args.private and not args.create_repo:
        parser.error("--private applies only to repository creation")
    if not (args.deb or args.directory or args.create_repo or args.export_key):
        parser.error("Choose --deb, --directory, --create-repo or --export-key")
    return args


def main():
    args = parse_args()
    counts = {"uploaded": 0, "skipped": 0, "failed": 0}
    try:
        token = os.environ.get("ARTIFACT_KEEPER_TOKEN")
        if not token:
            raise ValueError("Set ARTIFACT_KEEPER_TOKEN (never passed on the command line)")
        files = sorted(args.directory.rglob("*.deb")) if args.directory else args.deb or []
        if args.directory and not files:
            raise ValueError(f"No .deb files found in {args.directory}")
        client = Client(args.artifact_keeper, token, args.ca_bundle, args.http_timeout)
        _, key = ensure_repository(client, args.repo, args.create_repo, args.private)
        if args.export_key:
            public = key["public_key_pem"]
            if "BEGIN PGP PUBLIC KEY BLOCK" not in public:
                raise ValueError("Server did not return an armored OpenPGP public key")
            args.export_key.parent.mkdir(parents=True, exist_ok=True)
            args.export_key.write_text(public)
            args.export_key.chmod(0o644)
            print(f"Exported public key: {args.export_key}")
        for path in files:
            try:
                counts[upload(client, args.repo, args.component, path)] += 1
            except (OSError, ValueError, RuntimeError, tarfile.TarError, subprocess.SubprocessError) as exc:
                counts["failed"] += 1
                print(f"ERROR: {path.name}: {exc}", file=sys.stderr)
    except (OSError, ValueError, RuntimeError) as exc:
        counts["failed"] += 1
        print(f"ERROR: {exc}", file=sys.stderr)
    print("\n" + "=" * 60 + "\nSummary\n" + "=" * 60)
    print(f"Total:    {sum(counts.values())}\nUploaded: {counts['uploaded']}\nSkipped:  {counts['skipped']}\nFailed:   {counts['failed']}")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
