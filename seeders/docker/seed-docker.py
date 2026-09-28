#!/usr/bin/env python3

"""
Build or mirror Docker images into Artifact Keeper using Python 3.9+ and Docker.
See README.md for repository naming, authentication, TLS and pull examples.

Set ARTIFACT_KEEPER_TOKEN and AK_API=https://localhost/docker (repository docker).
Select --dockerfile, --docker-compose-file or --image. Dockerfile mode requires
--destination internal/name:tag, relative to the target repository namespace.
Docker Compose resolves interpolation, includes, overrides and all profiles;
services with build definitions are built, other service images are pulled.

This is a single-platform seeder, not a complete registry replication tool.
The platform defaults to the Docker daemon's OS/architecture. Docker 28+ is
required for explicit platform inspection/push, and Compose v2+ for Compose mode.
Docker manages TLS trust; Python CA environment variables do not configure it.
Source credentials use your normal Docker configuration. Target credentials are
kept in a temporary configuration and removed after the run. Images/build cache
remain in Docker; the script does not prune them or start Compose services.
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
import uuid

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
    return source, f"{registry.replace(':', '-port-')}/{path}:{target_tag}"


def validate_destination(value: str):
    if "@" in value or ":" not in value.rsplit("/", 1)[-1]:
        raise ValueError("--destination must be a repository-relative name:tag, not a digest")
    name, tag = value.rsplit(":", 1)
    if not re.fullmatch(TAG, tag) or any(not re.fullmatch(NAME_COMPONENT, part) for part in name.split("/")):
        raise ValueError("Invalid repository-relative destination name:tag")
    return value


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download or build Docker images and publish them to Artifact Keeper."
    )
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--dockerfile", type=Path, help="Dockerfile for an internal image")
    inputs.add_argument("--docker-compose-file", type=Path, action="append", help="Compose file; repeat to merge override files")
    inputs.add_argument("--image", help="Image name, name:tag, or name@sha256:digest")

    parser.add_argument("--artifact-keeper", default=os.environ.get("AK_API"), help="Target https://host/repository-key (default: AK_API)")
    parser.add_argument("--destination", help="Target name:tag inside the repository; required with --dockerfile")
    parser.add_argument("--context", type=Path, help="Docker build context (default: Dockerfile parent)")
    parser.add_argument("--build-arg", action="append", default=[], help="Dockerfile build argument; repeat as needed")
    parser.add_argument("--target", help="Dockerfile build stage")
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
    if args.dockerfile and not args.destination:
        parser.error("--dockerfile requires --destination internal/name:tag")
    if args.docker_compose_file and args.destination:
        parser.error("--destination is only available for --image or --dockerfile")
    if (args.context or args.build_arg or args.target) and not args.dockerfile:
        parser.error("--context, --build-arg and --target require --dockerfile")
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
    for key in ("dockerfile", "context", "work_dir"):
        value = getattr(args, key)
        if value is not None:
            setattr(args, key, value.expanduser().resolve())
    args.docker_compose_file = [path.expanduser().resolve() for path in args.docker_compose_file or []]
    args.env_file = [path.expanduser().resolve() for path in args.env_file]
    for path in ([args.dockerfile] if args.dockerfile else []) + args.docker_compose_file + args.env_file:
        if not path.is_file():
            parser.error(f"File does not exist: {path}")
    if args.dockerfile:
        args.context = args.context or args.dockerfile.parent
        if not args.context.is_dir():
            parser.error(f"Build context does not exist: {args.context}")
        if args.work_dir.is_relative_to(args.context):
            parser.error("--work-dir must be outside the build context")
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
        if "build" in configuration:
            context = configuration["build"].get("context", "")
            if context and Path(context).is_absolute() and args.work_dir.is_relative_to(Path(context).resolve()):
                raise ValueError(f"--work-dir must be outside the build context for service {service}")
            build_platforms = configuration["build"].get("platforms", [])
            if build_platforms and build_platforms != [platform]:
                raise ValueError(f"Service {service} build.platforms must match its single selected platform {platform}")
            source = source or f"{model['name']}-{service}:latest"
            _, destination = parse_image(source)
            if not configuration.get("image"):
                destination = validate_destination(f"internal/{model['name']}/{service}:latest")
            tasks.append({"source": source, "destination": destination, "platform": platform,
                          "build": [*command, "build", "--pull", service], "service": service})
        elif source:
            source, destination = parse_image(source)
            tasks.append({"source": source, "destination": destination, "platform": platform})
        else:
            raise ValueError(f"Compose service {service} has neither image nor build")
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

    if args.dockerfile:
        print("==> Mode: Dockerfile")
        build_reference = f"ak-seeder-build:{uuid.uuid4().hex}"
        command = [docker, "build", "--pull", "--load", "--file", str(args.dockerfile),
                   "--platform", default_platform, "--tag", build_reference]
        if args.target:
            command.extend(["--target", args.target])
        for argument in args.build_arg:
            command.extend(["--build-arg", argument])
        command.append(str(args.context))
        tasks = [{"destination": args.destination, "platform": default_platform,
                  "build": command, "build_reference": build_reference, "source": str(args.dockerfile)}]
    elif args.docker_compose_file:
        print("==> Mode: Docker Compose")
        tasks = compose_plan(args, docker, env, default_platform)
    else:
        print("==> Mode: single image")
        source, destination = parse_image(args.image)
        tasks = [{"source": source, "destination": args.destination or destination, "platform": default_platform}]

    # Resolve destination collisions before pulling, building or publishing.
    unique = {}
    skipped = 0
    for task in tasks:
        key = task["destination"]
        if key in unique:
            previous = unique[key]
            if any(previous.get(field) != task.get(field) for field in ("source", "platform", "build")):
                raise ValueError(f"Different images/platforms map to {key}; give them distinct image tags")
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
            if task.get("build"):
                print("==> Building image")
                run_command(task["build"], env=task_env, timeout=args.command_timeout)
            else:
                print("==> Downloading image")
                run_command([docker, "pull", "--platform", task["platform"], task["source"]], env=task_env, timeout=args.command_timeout)

            reference = task.get("build_reference") or task["source"]
            image_id = run_command(
                [docker, "image", "inspect", "--platform", task["platform"], "--format", "{{.Id}}", reference],
                env=task_env, timeout=args.command_timeout,
            ).strip()
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                raise RuntimeError("Docker returned an invalid image ID")
            # A containerd image store may not resolve the config digest as an
            # image reference. Tag the named image and select its platform on push.
            run_command([docker, "tag", reference, destination], env=task_env, timeout=args.command_timeout)
            print("Uploading to Artifact Keeper...")
            # Create credentials only after building; no build context should
            # accidentally capture the temporary authentication configuration.
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
