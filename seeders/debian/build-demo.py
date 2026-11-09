#!/usr/bin/env python3
"""Build deterministic sample .deb files with Python 3.10+ only (no root/compiler)."""

import argparse
import gzip
import io
import re
import tarfile
from pathlib import Path


def tar_gz(files):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data, mode in files:
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(data), mode
            entry.uid = entry.gid = entry.mtime = 0
            entry.uname = entry.gname = "root"
            archive.addfile(entry, io.BytesIO(data))
    return gzip.compress(stream.getvalue(), mtime=0)


def ar_member(name, data):
    header = f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n"
    return header.encode("ascii") + data + (b"\n" if len(data) % 2 else b"")


def build(output, package, version, architecture):
    if package not in ("ak-devtools-hello", "ak-devtools-info"):
        raise ValueError("Unknown demo package")
    if not re.fullmatch(r"[0-9][A-Za-z0-9.+~\-]*", version):
        raise ValueError("Demo version must start with a digit; use e.g. 1.0.0-1")
    if architecture not in ("amd64", "arm64"):
        raise ValueError("Demo architecture must be amd64 or arm64")
    command = package.removeprefix("ak-")
    message = "Hello from Artifact Keeper" if package.endswith("hello") else "Artifact Keeper development tools"
    payload = (
        f"#!/bin/sh\nset -eu\nprintf '%s\\n' '{message}' 'Package: {package}' "
        f"'Version: {version}' 'Built for: {architecture}'\nprintf 'Runtime: '\nuname -m\n"
    ).encode()
    control = (
        f"Package: {package}\nVersion: {version}\nArchitecture: {architecture}\n"
        "Maintainer: Devtools Demo <devtools@example.invalid>\nSection: devel\nPriority: optional\n"
        "Depends: dash\nInstalled-Size: 2\n"
        f"Description: {message} demo\n Shell payload for testing signed APT package delivery.\n"
    ).encode()
    members = (("debian-binary", b"2.0\n"),
               ("control.tar.gz", tar_gz([("./control", control, 0o644)])),
               ("data.tar.gz", tar_gz([(f"./usr/bin/{command}", payload, 0o755)])))
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"{package}_{version}_{architecture}.deb"
    path.write_bytes(b"!<arch>\n" + b"".join(ar_member(name, data) for name, data in members))
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".local/apt-local-devtools/packages"))
    parser.add_argument("--versions", nargs="+", default=["1.0.0-1"])
    parser.add_argument("--architectures", nargs="+", choices=["amd64", "arm64"], default=["amd64", "arm64"])
    args = parser.parse_args()
    for version in args.versions:
        for architecture in args.architectures:
            for package in ("ak-devtools-hello", "ak-devtools-info"):
                print(build(args.output, package, version, architecture))


if __name__ == "__main__":
    main()
