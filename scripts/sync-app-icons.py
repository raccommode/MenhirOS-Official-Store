#!/usr/bin/env python3
"""Attach upstream application icons to every manifest in the official store."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import pathlib
import re
import urllib.request

import yaml


LINUXSERVER_API = "https://api.linuxserver.io/api/v1/images?include_config=true"
APP_ID_ALIASES = {
    "homeassistant": "home-assistant",
    "pyload": "pyload-ng",
}
ICON_OVERRIDES = {
    "bitcoin-core": "https://cdn.jsdelivr.net/gh/selfhst/icons/svg/bitcoin.svg",
    "faster-whisper": "https://cdn.jsdelivr.net/gh/selfhst/icons/svg/openai.svg",
    "foldingathome": "https://api.iconify.design/mdi:molecule.svg?color=%238b5cf6",
    "phoenixd": "https://api.iconify.design/mdi:lightning-bolt.svg?color=%23f5a623",
    "projectsend": "https://cdn.jsdelivr.net/gh/selfhst/icons/svg/projectsend.svg",
    "thelounge": "https://cdn.jsdelivr.net/gh/selfhst/icons/svg/the-lounge.svg",
    "xbackbone": "https://cdn.jsdelivr.net/gh/selfhst/icons/svg/xbackbone.svg",
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("casaos_source", type=pathlib.Path, help="CasaOS-AppStore checkout")
    return parser.parse_args()


def slug(value: str) -> str:
    value = html.unescape(value).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return APP_ID_ALIASES.get(value, value)


def localized_title(meta: dict, fallback: str) -> str:
    values = meta.get("title")
    if isinstance(values, dict):
        for language in ("en_US", "en_GB", "en"):
            if values.get(language):
                return str(values[language])
    return fallback


def normalize_icon(value: object) -> str:
    icon = str(value or "").strip()
    if icon.startswith("http://"):
        icon = "https://" + icon.removeprefix("http://")
    return icon


def casaos_icons(source: pathlib.Path) -> dict[str, str]:
    icons: dict[str, str] = {}
    for path in sorted((source / "Apps").glob("*/docker-compose.yml")):
        compose = yaml.safe_load(path.read_text(encoding="utf-8"))
        meta = compose.get("x-casaos") or {}
        app_id = slug(localized_title(meta, path.parent.name))
        icon = normalize_icon(meta.get("icon"))
        if icon.startswith("https://"):
            icons[app_id] = icon
    return icons


def linuxserver_icons() -> dict[str, str]:
    request = urllib.request.Request(LINUXSERVER_API, headers={"User-Agent": "MenhirOS-store-sync/1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    icons: dict[str, str] = {}
    for item in payload["data"]["repositories"]["linuxserver"]:
        app_id = slug(item["name"])
        icon = normalize_icon(item.get("project_logo"))
        if icon.startswith("https://"):
            icons[app_id] = icon
    return icons


def with_icon(manifest: dict, icon: str) -> dict:
    updated: dict = {}
    for key, value in manifest.items():
        if key == "icon":
            continue
        updated[key] = value
        if key == "description":
            updated["icon"] = icon
    if "icon" not in updated:
        updated["icon"] = icon
    return updated


def main() -> int:
    options = arguments()
    if not (options.casaos_source / "Apps").is_dir():
        raise RuntimeError(f"{options.casaos_source} is not a CasaOS-AppStore checkout")

    icons = linuxserver_icons()
    icons.update(casaos_icons(options.casaos_source))
    icons.update(ICON_OVERRIDES)

    index_path = pathlib.Path("store.yaml")
    index = yaml.safe_load(index_path.read_text(encoding="utf-8"))
    missing: list[str] = []
    for reference in index["apps"]:
        app_id = reference["id"]
        icon = icons.get(app_id, "")
        if not icon.startswith("https://"):
            missing.append(app_id)
            continue
        path = pathlib.Path(reference["path"])
        manifest = with_icon(yaml.safe_load(path.read_text(encoding="utf-8")), icon)
        content = yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False, width=1000)
        path.write_text(content, encoding="utf-8")
        reference["sha256"] = hashlib.sha256(content.encode()).hexdigest()

    if missing:
        raise RuntimeError("No icon found for: " + ", ".join(missing))

    index_path.write_text(
        yaml.safe_dump(index, allow_unicode=True, sort_keys=False, width=1000),
        encoding="utf-8",
    )
    print(f"Attached HTTPS icons to all {len(index['apps'])} store applications.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
