#!/usr/bin/env python3
"""Upload an unpacked p2 site with explicit JAR package coordinates.

Example:
  python3 scripts/upload-p2.py /path/to/site --repository p2 \
      --token-file /tmp/ak-upload-token
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile
import xml.etree.ElementTree as ET
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


def coordinates(path):
    if path.parts[0] in ("plugins", "features") and path.suffix == ".jar":
        name, version = path.stem.rsplit("_", 1)
        if not name or not version:
            raise ValueError(f"Invalid p2 JAR filename: {path}")
        return name, version
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site", type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--url", default="http://localhost")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    files = sorted(p for p in args.site.rglob("*") if p.is_file())
    if not files or not (args.site / "p2.index").is_file():
        parser.error("site must be an unpacked p2 repository containing p2.index")
    # GitHub-hosted p2 sites can map bundles and features to root-level JARs.
    # Read their declared identities instead of treating these as metadata JARs.
    flat_coordinates = {}
    if (args.site / "artifacts.jar").is_file():
        with zipfile.ZipFile(args.site / "artifacts.jar") as archive:
            metadata = ET.fromstring(archive.read("artifacts.xml"))
        flat_classifiers = set()
        for rule in metadata.findall("./mappings/rule"):
            if rule.get("output") == "${repoUrl}/${id}_${version}.jar":
                for classifier in ("osgi.bundle", "org.eclipse.update.feature"):
                    if f"classifier={classifier}" in rule.get("filter", ""):
                        flat_classifiers.add(classifier)
        for artifact in metadata.findall("./artifacts/artifact"):
            if artifact.get("classifier") in flat_classifiers:
                name, version = artifact.attrib["id"], artifact.attrib["version"]
                flat_coordinates[f"{name}_{version}.jar"] = (name, version)

    def site_coordinates(path):
        return flat_coordinates.get(path.as_posix()) or coordinates(path)

    expected = {site_coordinates(p.relative_to(args.site)) for p in files}
    expected.discard(None)
    # Publish payloads before metadata that advertises them.
    files.sort(key=lambda p: site_coordinates(p.relative_to(args.site)) is None)
    print(f"{len(files)} files, {len(expected)} distinct package coordinates", flush=True)
    if args.dry_run:
        return
    if args.token_file is None:
        parser.error("--token-file is required for upload")
    token = args.token_file.read_text().strip()

    def api(method, path, data=None, headers=None):
        hdr = {"Authorization": f"Bearer {token}", **(headers or {})}
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            hdr["Content-Type"] = "application/json"
        request = Request(args.url.rstrip("/") + path, data=data, headers=hdr, method=method)
        try:
            with urlopen(request, timeout=120) as response:
                body = response.read()
                return json.loads(body) if body else None
        except HTTPError as exc:
            raise RuntimeError(f"{method} {path}: HTTP {exc.code}: {exc.read().decode()}") from None

    repo = api("GET", "/api/v1/repositories/" + quote(args.repository, safe=""))
    if repo["format"] != "p2":
        raise ValueError("Target repository must have p2 format")
    for index, file in enumerate([] if args.verify_only else files, 1):
        path = file.relative_to(args.site)
        content = file.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        spec = {
            "repository_key": args.repository,
            "artifact_path": path.as_posix(),
            "total_size": len(content),
            "checksum_sha256": digest,
            "chunk_size": 8 * 1024 * 1024,
            "content_type": "application/java-archive" if file.suffix == ".jar" else "application/octet-stream",
        }
        coord = site_coordinates(path)
        if coord:
            spec.update(artifact_name=coord[0], artifact_version=coord[1])
        session = api("POST", "/api/v1/uploads", spec)
        endpoint = "/api/v1/uploads/" + session["session_id"]
        chunk_size = session["chunk_size"]
        for start in range(0, len(content), chunk_size):
            chunk = content[start:start + chunk_size]
            api("PATCH", endpoint, chunk, {
                "Content-Type": "application/octet-stream",
                "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{len(content)}",
            })
        result = api("PUT", endpoint + "/complete")
        if result["checksum_sha256"] != digest:
            raise RuntimeError(f"Checksum mismatch for {path}")
        print(f"[{index}/{len(files)}] {path}", flush=True)

    found = set()
    page = 1
    while True:
        query = urlencode({"repository_key": args.repository, "per_page": 100, "page": page})
        result = api("GET", "/api/v1/packages?" + query)
        items = result["items"]
        for item in items:
            versions = api("GET", f"/api/v1/packages/{item['id']}/versions")
            found.update((item["name"], version["version"]) for version in versions["versions"])
        if len(items) < 100:
            break
        page += 1
    missing = expected - found
    if missing:
        raise RuntimeError(f"Uploaded, but package verification failed: {sorted(missing)}")
    if not args.verify_only:
        print(f"Verified {len(files)} upload checksums.")
    print(f"Verified {len(expected)} indexed package coordinates.")
    print(f"Update site: {args.url.rstrip('/')}/api/v1/repositories/{quote(args.repository, safe='')}/download/")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
