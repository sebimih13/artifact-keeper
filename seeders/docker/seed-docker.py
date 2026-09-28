#!/usr/bin/env python3

"""
Publish existing local Docker images to Artifact Keeper; never build or pull.
Requires Python 3.9+, Docker 28+, and Compose v2+ for Compose input.

Set ARTIFACT_KEEPER_TOKEN and AK_API=https://localhost/docker.
Use --image existing-name:tag or --docker-compose-file compose.yml.
All images and the selected platform must already exist in the same daemon.
Compose build-only services use the existing <project>-<service>:latest image;
Compose build definitions are never executed. Use --project-name if the images
were originally built with docker compose -p NAME.

Docker Hub names map to library/name or namespace/name inside the repository,
so AK_DEFAULT_DOCKER_MIRROR_REPO=docker can serve unchanged Docker Hub names.
Other registry names retain their registry prefix and require explicit pulls
from Artifact Keeper; Docker's registry-mirrors setting only mirrors Docker Hub.

Target credentials are temporary. Existing daemon/context selection is preserved.
Single-platform publication does not preserve multi-platform index digests,
signatures or attestations. See README.md for mirror authentication and TLS.
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

from pathlib import Path


DEFAULT_WORK_DIR = "/tmp/artifact-keeper-docker-seed"
COMMAND_TIMEOUT = 1800
NAME_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
TAG = r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}"


def parse_image(reference: str):
    if not reference or reference.startswith("-") or "://" in reference:
        raise ValueError("Use an image reference, not a URL or Docker option")

    name, separator, digest = reference.partition("@")
    if separator and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Digest references must contain sha256: followed by 64 lowercase hex digits")

    tag = "latest"
    if ":" in name.rsplit("/", 1)[-1]:
        name, tag = name.rsplit(":", 1)
    if not re.fullmatch(TAG, tag):
        raise ValueError(f"Invalid image tag: {tag}")

    parts = name.split("/")
    registry = "docker.io"
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry = parts.pop(0).lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*(?::[0-9]+)?", registry):
            raise ValueError(f"Unsupported registry host: {registry}")
    if registry in ("index.docker.io", "registry-1.docker.io"):
        registry = "docker.io"
    if registry == "docker.io" and len(parts) == 1:
        parts.insert(0, "library")
    if not parts or any(not re.fullmatch(NAME_COMPONENT, part) for part in parts):
        raise ValueError(f"Invalid image repository: {reference}")

    path = "/".join(parts)
    source = f"{registry}/{path}" + (f"@{digest}" if separator else f":{tag}")
    target_tag = digest.replace(":", "-", 1) if separator else tag
    destination = f"{path}:{target_tag}"
    if registry != "docker.io":
        destination = f"{registry.replace(':', '-port-')}/{destination}"
    return source, destination


def validate_destination(value: str):
    if "@" in value or ":" not in value.rsplit("/", 1)[-1]:
        raise ValueError("--destination must be a repository-relative name:tag, not a digest")
    name, tag = value.rsplit(":", 1)
    if not re.fullmatch(TAG, tag) or any(not re.fullmatch(NAME_COMPONENT, part) for part in name.split("/")):
        raise ValueError("Invalid repository-relative destination name:tag")
    return value


def parse_args():
    parser = argparse.ArgumentParser(
        description="Publish images already present in the Docker daemon to Artifact Keeper."
    )
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--docker-compose-file", type=Path, action="append", help="Compose file; repeat to merge override files")
    inputs.add_argument("--image", help="Existing local image name, name:tag, or name@sha256:digest")

    parser.add_argument("--artifact-keeper", default=os.environ.get("AK_API"), help="Target https://host/repository-key (default: AK_API)")
    parser.add_argument("--destination", help="Target name:tag inside the repository (single-image mode only)")
    parser.add_argument("--project-name", help="Compose project name used when the images were built")
    parser.add_argument("--platform", help="One OS/architecture[/variant]; default: Docker daemon platform")
    parser.add_argument("--env-file", type=Path, action="append", default=[], help="Compose interpolation env file; repeat as needed")
    parser.add_argument("--work-dir", type=Path, default=Path(DEFAULT_WORK_DIR))
    parser.add_argument("--command-timeout", type=int, default=COMMAND_TIMEOUT)
    args = parser.parse_args()

    if not args.artifact_keeper:
        parser.error("--artifact-keeper or AK_API is required")
    url = urllib.parse.urlsplit(args.artifact_keeper)
    if (
        url.scheme != "https" or not url.hostname or url.username or url.password
        or url.query or url.fragment or not re.fullmatch(r"/[a-z0-9][a-z0-9_-]*/?", url.path)
        or any(character.isspace() for character in args.artifact_keeper)
    ):
        parser.error("Use https://host/repository-key, for example https://localhost/docker; /npm/npm is not a Docker endpoint")
    args.registry = url.netloc
    args.repository = url.path.strip("/")
    if args.docker_compose_file and args.destination:
        parser.error("--destination is only available for --image")
    if args.project_name and not args.docker_compose_file:
        parser.error("--project-name requires --docker-compose-file")
    if args.env_file and not args.docker_compose_file:
        parser.error("--env-file requires --docker-compose-file")
    if args.platform and not re.fullmatch(r"[a-z0-9]+/[a-z0-9]+(?:/[a-z0-9]+)?", args.platform):
        parser.error("--platform must be a single OS/architecture[/variant]")
    if args.command_timeout <= 0:
        parser.error("--command-timeout must be positive")

    if args.image:
        parse_image(args.image)
    elif args.image == "":
        parser.error("--image must not be empty")
    if args.destination:
        validate_destination(args.destination)
    for key in ("work_dir",):
        value = getattr(args, key)
        if value is not None:
            setattr(args, key, value.expanduser().resolve())
    args.docker_compose_file = [path.expanduser().resolve() for path in args.docker_compose_file or []]
    args.env_file = [path.expanduser().resolve() for path in args.env_file]
    for path in args.docker_compose_file + args.env_file:
        if not path.is_file():
            parser.error(f"File does not exist: {path}")
    return args


def run_command(command, *, env, timeout, cwd=None):
    try:
        return subprocess.run(
            command, cwd=cwd, env=env, timeout=timeout, check=True,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Docker command timed out after {timeout}s") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Docker command failed (exit {exc.returncode}):\n{exc.stderr or exc.stdout}") from exc


def docker_environment():
    env = os.environ.copy()
    env.pop("ARTIFACT_KEEPER_TOKEN", None)
    return env


def target_configuration(directory: Path, registry: str, token: str):
    original = Path(os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker"))).expanduser().resolve()
    source = {}
    if (original / "config.json").is_file():
        source = json.loads((original / "config.json").read_text(encoding="utf-8"))
    configuration = {
        "auths": {registry: {"auth": base64.b64encode(f"__token__:{token}".encode()).decode()}},
    }
    if source.get("currentContext"):
        configuration["currentContext"] = source["currentContext"]
    if (original / "contexts").is_dir():
        (directory / "contexts").symlink_to(original / "contexts", target_is_directory=True)
    # No credential-store helpers: the temporary credential must stay temporary.
    with os.fdopen(os.open(directory / "config.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(configuration, stream)


def compose_plan(args, docker, env, default_platform):
    command = [docker, "compose"]
    if args.project_name:
        command.extend(["--project-name", args.project_name])
    for path in args.docker_compose_file:
        command.extend(["--file", str(path)])
    for path in args.env_file:
        command.extend(["--env-file", str(path)])
    command.extend(["--profile", "*"])
    model = json.loads(run_command(
        [*command, "config", "--format", "json"], env=env, timeout=args.command_timeout,
    ))
    tasks = []
    for service, configuration in model.get("services", {}).items():
        platform = configuration.get("platform") or args.platform or default_platform
        if args.platform and configuration.get("platform") and args.platform != configuration["platform"]:
            raise ValueError(f"Service {service} platform conflicts with --platform")
        source = configuration.get("image")
        if not source and "build" in configuration:
            source = f"{model['name']}-{service}:latest"
        if not source:
            raise ValueError(f"Compose service {service} has neither image nor build")
        source, destination = parse_image(source)
        tasks.append({"source": source, "destination": destination, "platform": platform})
    if not tasks:
        raise ValueError("No service images were found in the Compose model")
    return tasks


def seed(args, docker: str, token: str, directory: Path):
    env = docker_environment()
    platform = run_command(
        [docker, "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"],
        env=env, timeout=args.command_timeout,
    ).strip()
    default_platform = args.platform or platform
    if not re.fullmatch(r"[a-z0-9]+/[a-z0-9]+(?:/[a-z0-9]+)?", default_platform):
        raise RuntimeError(f"Could not determine the Docker daemon platform: {platform}")

    if args.docker_compose_file:
        print("==> Mode: Docker Compose (local images)")
        tasks = compose_plan(args, docker, env, default_platform)
    else:
        print("==> Mode: single image")
        source, destination = parse_image(args.image)
        tasks = [{"source": source, "destination": args.destination or destination, "platform": default_platform}]

    # Resolve destination collisions before publishing.
    unique = {}
    skipped = 0
    for task in tasks:
        key = task["destination"]
        if key in unique:
            previous = unique[key]
            if any(previous.get(field) != task.get(field) for field in ("source", "platform")):
                raise ValueError(f"Different local images/platforms map to {key}; give them distinct image tags")
            print(f"SKIP: duplicate Compose image {task['source']}")
            skipped += 1
            continue
        unique[key] = task

    uploaded = failed = 0
    print(f"    Repository: {args.registry}/{args.repository}")
    print()

    for task in unique.values():
        destination = f"{args.registry}/{args.repository}/{task['destination']}"
        print("-" * 60)
        print(f"Image: {task['source']}")
        print(f"Artifact: {destination}")
        print(f"Platform: {task['platform']}")
        try:
            task_env = env.copy()
            task_env["DOCKER_DEFAULT_PLATFORM"] = task["platform"]
            print("==> Inspecting existing local image")
            reference = task["source"]
            try:
                image_id = run_command(
                    [docker, "image", "inspect", "--platform", task["platform"], "--format", "{{.Id}}", reference],
                    env=task_env, timeout=args.command_timeout,
                ).strip()
            except RuntimeError as exc:
                raise RuntimeError(
                    f"Cannot inspect local image {reference} for {task['platform']}. "
                    f"Build or load it in this Docker daemon before seeding.\n{exc}"
                ) from exc
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                raise RuntimeError("Docker returned an invalid image ID")
            # A containerd image store may not resolve the config digest as an
            # image reference. Tag the named image and select its platform on push.
            run_command([docker, "tag", reference, destination], env=task_env, timeout=args.command_timeout)
            print("Uploading to Artifact Keeper...")
            # Keep target credentials isolated from the user's Docker config.
            with tempfile.TemporaryDirectory(prefix="auth-", dir=directory) as auth_directory:
                auth = Path(auth_directory)
                target_configuration(auth, args.registry, token)
                output = run_command(
                    [docker, "--config", str(auth), "push", "--platform", task["platform"], destination],
                    env=task_env, timeout=args.command_timeout,
                )
            print(f"UPLOADED: {destination}")
            digests = re.findall(r"digest: (sha256:[0-9a-f]{64})", output)
            if digests:
                print(f"Pull: {destination.rsplit(':', 1)[0]}@{digests[-1]}")
            uploaded += 1
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            failed += 1
        print()

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
    try:
        args = parse_args()
        token = os.environ.get("ARTIFACT_KEEPER_TOKEN")
        if not token:
            raise ValueError("ARTIFACT_KEEPER_TOKEN is not set")
        docker = shutil.which(os.environ.get("DOCKER", "docker"))
        if not docker:
            raise ValueError("Docker executable was not found")
        args.work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="run-", dir=args.work_dir) as directory:
            return seed(args, docker, token, Path(directory))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
