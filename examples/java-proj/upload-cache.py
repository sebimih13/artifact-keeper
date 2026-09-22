#!/usr/bin/env python3
"""Import the JARs and POMs from a dedicated Maven staging cache.

Run after the staging build: python3 upload-cache.py .m2-staging
The destination must be a hosted (local) Maven repository. Existing files
are verified instead of overwritten. Every API request is printed.
"""

import argparse
import getpass
import hashlib
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cache", type=Path)
    parser.add_argument("--url", default="http://localhost")
    parser.add_argument("--repository", default="maven")
    parser.add_argument("--username", default="admin")
    args = parser.parse_args()
    files = sorted(p for p in args.cache.rglob("*") if p.is_file() and p.suffix in {".jar", ".pom"})
    if not files:
        parser.error("Cache contains no JARs or POMs")

    token = None

    def request(method, path, data=None, content_type="application/json"):
        print(f"{method} {args.url.rstrip('/')}{path}", flush=True)
        headers = {"Content-Type": content_type}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if isinstance(data, dict):
            data = json.dumps(data).encode()
        req = Request(args.url.rstrip("/") + path, data=data, headers=headers, method=method)
        with urlopen(req, timeout=180) as response:
            return response.read()

    password = getpass.getpass(f"Artifact Keeper password for {args.username}: ")
    token = json.loads(request("POST", "/api/v1/auth/login", {
        "username": args.username, "password": password,
    }))["access_token"]
    key = quote(args.repository, safe="")
    try:
        repo = json.loads(request("GET", f"/api/v1/repositories/{key}"))
    except HTTPError as error:
        if error.code != 404:
            raise
        repo = json.loads(request("POST", "/api/v1/repositories", {
            "key": args.repository,
            "name": "Maven Dependencies",
            "description": "Hosted Maven dependencies and build plugins for offline builds",
            "format": "maven", "repo_type": "local", "is_public": True,
        }))
    if repo.get("format") != "maven" or repo.get("repo_type") != "local":
        raise RuntimeError("Destination must be a local Maven repository")

    total = 0
    for source in files:
        relative = source.relative_to(args.cache).as_posix()
        path = f"/maven/{key}/" + quote(relative, safe="/")
        content = source.read_bytes()
        try:
            hosted = request("GET", path)
        except HTTPError as error:
            if error.code != 404:
                raise
            request("PUT", path, content, "application/octet-stream")
            hosted = request("GET", path)
        if hashlib.sha256(content).digest() != hashlib.sha256(hosted).digest():
            raise RuntimeError(f"Hosted file differs from staging file: {relative}")
        total += len(content)
    print(f"Verified {len(files)} files ({total:,} bytes) in {args.url.rstrip('/')}/maven/{key}/")


if __name__ == "__main__":
    main()
