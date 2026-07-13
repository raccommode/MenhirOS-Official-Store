#!/usr/bin/env python3
import json
import pathlib
import subprocess
import sys

import yaml


def platforms_for(image: str) -> set[str]:
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", image, "--format", "{{json .Manifest}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    manifest = json.loads(result.stdout)
    return {
        item.get("platform", {}).get("architecture")
        for item in manifest.get("manifests", [])
        if item.get("platform", {}).get("os") == "linux"
    }


def main() -> int:
    checked: dict[str, set[str]] = {}
    for path in sorted(pathlib.Path("apps").glob("*/app.yaml")):
        app = yaml.safe_load(path.read_text())
        required = set(app["architectures"])
        for container in app["containers"]:
            image = container["image"]
            try:
                available = checked.setdefault(image, platforms_for(image))
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
                print(f"{path}: cannot inspect {image}: {error}", file=sys.stderr)
                return 1
            missing = required - available
            if missing:
                print(f"{path}: {image} is missing declared Linux architectures: " + ", ".join(sorted(missing)), file=sys.stderr)
                return 1
            print(f"{path}: verified {image} for {', '.join(sorted(required))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
