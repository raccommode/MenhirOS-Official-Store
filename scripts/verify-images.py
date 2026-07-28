#!/usr/bin/env python3
import json
import pathlib
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml


def platforms_for(image: str) -> set[str]:
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
    return {
        item.get("platform", {}).get("architecture")
        for item in manifest.get("manifests", [])
        if item.get("platform", {}).get("os") == "linux"
    }


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
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
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
