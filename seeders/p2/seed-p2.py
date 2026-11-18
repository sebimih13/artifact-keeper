#!/usr/bin/env python3
"""Upload an already built Eclipse P2 update site without changing its files.

AK_API_TOKEN=<token> python3 seed-p2.py --dir /path/to/site \
    --artifact-keeper https://localhost/api/v1/repositories/egit-local

Python 3.9+, standard library only. Target: a local P2 repository.
"""

import argparse
import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path

DEFAULT_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
HTTP_TIMEOUT = 60
METADATA_FILES = ("artifacts.jar", "artifacts.xml.xz", "artifacts.xml", "content.jar", "content.xml.xz", "content.xml", "p2.index")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        fp.close()
        raise RuntimeError("Repository redirected the request; use its final API URL")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, required=True, help="Built update site containing content/artifacts metadata")
    parser.add_argument("--artifact-keeper", required=True, help="Full repository API URL: https://host/api/v1/repositories/<key>")
    parser.add_argument("--ca-bundle", type=Path, help="Optional PEM CA bundle (default: system trust store)")
    parser.add_argument("--http-timeout", type=int, default=HTTP_TIMEOUT)
    args = parser.parse_args()
    args.dir = args.dir.expanduser().resolve()
    if not args.dir.is_dir():
        parser.error(f"Site directory does not exist: {args.dir}")
    url = urllib.parse.urlsplit(args.artifact_keeper)
    if (url.scheme not in ("https", "http") or not url.hostname or url.username or url.password
            or url.query or url.fragment or not re.fullmatch(r"/api/v1/repositories/[A-Za-z0-9_-]+/?", url.path)):
        parser.error("--artifact-keeper must be https://host/api/v1/repositories/<key>, without credentials")
    args.artifact_keeper = args.artifact_keeper.rstrip("/")
    if args.http_timeout <= 0:
        parser.error("Timeout must be positive")
    if args.ca_bundle:
        args.ca_bundle = args.ca_bundle.expanduser().resolve()
        if not args.ca_bundle.is_file():
            parser.error(f"CA bundle does not exist: {args.ca_bundle}")
    return args


def site_files(site):
    for stem in ("content", "artifacts"):
        if not any((site / (stem + suffix)).is_file() for suffix in (".jar", ".xml", ".xml.xz")):
            raise RuntimeError(f"Missing {stem} metadata in {site}; provide a built update site")
    paths = []
    for path in sorted(site.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"Refusing to upload symlink: {path}")
        if path.is_file():
            relative = path.relative_to(site)
            coordinates(relative)  # Validate bundle filenames before uploading anything.
            paths.append(relative)
    # Upload payloads before the metadata advertising them; publish discovery last.
    return sorted(paths, key=lambda p: (p.name in METADATA_FILES, p.name == "p2.index"))


def coordinates(path):
    if path.parent.name in ("plugins", "features") and path.suffix == ".jar":
        name, separator, version = path.stem.rpartition("_")
        if not separator or not name or not version:
            raise RuntimeError(f"Invalid P2 bundle/feature filename: {path}")
        return {"artifact_name": name, "artifact_version": version}
    return {}


def sha256(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def request(opener, url, token, timeout, *, method="GET", data=None, headers=None):
    headers = {"Authorization": f"Bearer {token}", **(headers or {})}
    if isinstance(data, dict):
        data = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    for attempt in range(5):
        try:
            return opener.open(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 4:
                raise
            retry_after = exc.headers.get("Retry-After", "")
            exc.close()
            try:
                delay = float(int(retry_after))
            except ValueError:
                try:
                    delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    delay = 2 ** (attempt + 1)
            delay = max(1, delay)
            if delay > 300:
                raise RuntimeError(f"Server requested a {delay:.0f}s wait; retry publication later") from exc
            print(f"Rate limited; retrying in {delay:.0f}s ({attempt + 1}/4)", flush=True)
            time.sleep(delay)


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


def remote_inventory(opener, args, token):
    inventory = {}
    page = 1
    cursor = None
    seen_cursors = set()
    while True:
        query = {"per_page": 100, "page": page}
        if cursor:
            query["cursor"] = cursor
        url = args.artifact_keeper + "/artifacts?" + urllib.parse.urlencode(query)
        with request(opener, url, token, args.http_timeout) as response:
            result = json.load(response)
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise RuntimeError("Invalid repository artifact inventory response")
        for item in result["items"]:
            path = item.get("path")
            digest = item.get("checksum_sha256")
            if not isinstance(path, str) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                raise RuntimeError("Repository inventory is missing a valid path or SHA-256")
            inventory[path] = digest.lower()
        more = result.get("has_more")
        if more is None:
            more = page < result.get("pagination", {}).get("total_pages", page)
        if not more:
            return inventory
        cursor = result.get("next_cursor")
        if cursor:
            if cursor in seen_cursors:
                raise RuntimeError("Repository inventory repeated its pagination cursor")
            seen_cursors.add(cursor)
        page += 1


def upload_file(opener, args, relative, token):
    path = args.dir / relative
    base = args.artifact_keeper.split("/repositories/", 1)[0] + "/uploads"
    spec = {
        "repository_key": args.artifact_keeper.rsplit("/", 1)[-1],
        "artifact_path": relative.as_posix(),
        "total_size": path.stat().st_size,
        "checksum_sha256": sha256(path),
        "chunk_size": 8 * 1024 * 1024,
        "content_type": "application/java-archive" if path.suffix == ".jar" else "application/octet-stream",
        **coordinates(relative),
    }
    with request(opener, base, token, args.http_timeout, method="POST", data=spec) as response:
        session = json.load(response)
    endpoint = base + "/" + urllib.parse.quote(session["session_id"], safe="")
    chunk_size = session["chunk_size"]
    if not isinstance(chunk_size, int) or chunk_size <= 0:
        raise RuntimeError("Invalid upload chunk size")
    with path.open("rb") as stream:
        start = 0
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            headers = {"Content-Type": "application/octet-stream", "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{spec['total_size']}"}
            with request(opener, endpoint, token, args.http_timeout, method="PATCH", data=chunk, headers=headers):
                pass
            start += len(chunk)
    with request(opener, endpoint + "/complete", token, args.http_timeout, method="PUT") as response:
        result = json.load(response)
    if result.get("checksum_sha256") != spec["checksum_sha256"]:
        raise RuntimeError(f"Upload checksum mismatch: {relative}")


def upload_site(args, paths, token):
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(args.ca_bundle) if args.ca_bundle else None)))
    with request(opener, args.artifact_keeper, token, args.http_timeout) as response:
        repository = json.load(response)
    if repository.get("format") != "p2" or repository.get("repo_type") != "local":
        raise RuntimeError("Use a local P2 repository")

    print("==> Checking existing artifacts")
    inventory = remote_inventory(opener, args, token)
    plan = []
    for relative in paths:
        encoded = urllib.parse.quote(relative.as_posix(), safe="/")
        local = args.dir / relative
        existing = inventory.get(relative.as_posix())
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
            upload_file(opener, args, relative, token)
            if remote_digest(opener, args.artifact_keeper + "/download/" + encoded, token, args.http_timeout) != sha256(args.dir / relative):
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
    print(f"Update site: {args.artifact_keeper}/download/")
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
        token = os.environ.get("AK_API_TOKEN") or os.environ.get("ARTIFACT_KEEPER_TOKEN")
        if not token:
            raise RuntimeError("AK_API_TOKEN is not set")
        paths = site_files(args.dir)
        return upload_site(args, paths, token)
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
