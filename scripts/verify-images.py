#!/usr/bin/env python3
import json
import pathlib
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml


def docker_hub_coordinates(image: str) -> tuple[str, str, str] | None:
    reference, separator, digest = image.partition("@")
    if not separator or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        return None
    first = reference.split("/", 1)[0]
    if "/" in reference and ("." in first or ":" in first or first == "localhost"):
        if first not in {"docker.io", "index.docker.io", "registry-1.docker.io"}:
            return None
        reference = reference.split("/", 1)[1]
    if "/" not in reference:
        reference = "library/" + reference
    last_slash = reference.rfind("/")
    last_colon = reference.rfind(":")
    if last_colon > last_slash:
        repository, tag = reference[:last_colon], reference[last_colon + 1 :]
    else:
        repository, tag = reference, "latest"
    return repository, tag, digest


def docker_hub_platforms(image: str) -> set[str] | None:
    coordinates = docker_hub_coordinates(image)
    if coordinates is None:
        return None
    repository, tag, _ = coordinates
    url = (
        "https://hub.docker.com/v2/repositories/"
        + "/".join(urllib.parse.quote(part, safe="") for part in repository.split("/"))
        + "/tags/"
        + urllib.parse.quote(tag, safe="")
    )
    request = urllib.request.Request(url, headers={"User-Agent": "MenhirOS-store-validator/1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    return {
        item.get("architecture")
        for item in payload.get("images") or []
        if item.get("os") == "linux"
    }


def platforms_for(image: str) -> set[str]:
    docker_hub = docker_hub_platforms(image)
    if docker_hub is not None:
        return docker_hub
    error: subprocess.CalledProcessError | subprocess.TimeoutExpired | None = None
    for attempt in range(3):
        try:
            result = subprocess.run(
                ["docker", "buildx", "imagetools", "inspect", image, "--format", "{{json .Manifest}}"],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as reason:
            error = reason
            if attempt < 2:
                time.sleep(2 ** attempt)
    else:
        assert error is not None
        raise error
    manifest = json.loads(result.stdout)
    architectures = {
        item.get("platform", {}).get("architecture")
        for item in manifest.get("manifests", [])
        if item.get("platform", {}).get("os") == "linux"
    }
    if architectures:
        return architectures
    image_result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", image, "--format", "{{json .Image}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    metadata = json.loads(image_result.stdout)
    if isinstance(metadata, dict) and metadata.get("architecture"):
        return {metadata["architecture"]}
    if isinstance(metadata, dict):
        return {
            key.split("/", 1)[1]
            for key in metadata
            if isinstance(key, str) and key.startswith("linux/")
        }
    return set()


def main() -> int:
    requirements: dict[str, set[str]] = {}
    sources: dict[str, list[pathlib.Path]] = {}
    for path in sorted(pathlib.Path("apps").glob("*/app.yaml")):
        app = yaml.safe_load(path.read_text())
        required = set(app["architectures"])
        for container in app["containers"]:
            image = container["image"]
            requirements.setdefault(image, set()).update(required)
            sources.setdefault(image, []).append(path)

    checked: dict[str, set[str]] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = {pool.submit(platforms_for, image): image for image in requirements}
        for future in as_completed(pending):
            image = pending[future]
            try:
                checked[image] = future.result()
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError, RuntimeError) as error:
                paths = ", ".join(str(path) for path in sources[image])
                print(f"{paths}: cannot inspect {image}: {error}", file=sys.stderr)
                return 1

    for image in sorted(requirements):
        required = requirements[image]
        available = checked[image]
        for path in sources[image]:
            missing = required - available
            if missing:
                print(f"{path}: {image} is missing declared Linux architectures: " + ", ".join(sorted(missing)), file=sys.stderr)
                return 1
            print(f"{path}: verified {image} for {', '.join(sorted(required))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
