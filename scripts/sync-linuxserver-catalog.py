#!/usr/bin/env python3
"""Refresh the reviewed LinuxServer.io applications shipped by Menhir."""

from __future__ import annotations

import concurrent.futures
import hashlib
import html
import json
import pathlib
import re
import subprocess
import urllib.request

import yaml


API_URL = "https://api.linuxserver.io/api/v1/images?include_config=true"
APP_IDS = [
    "radarr",
    "prowlarr",
    "sonarr",
    "qbittorrent",
    "github-desktop",
    "lidarr",
    "bazarr",
    "sabnzbd",
    "firefox",
    "rustdesk",
    "code-server",
    "chromium",
    "jackett",
    "speedtest-tracker",
    "homeassistant",
    "tautulli",
    "transmission",
    "heimdall",
    "syncthing",
    "calibre-web",
    "filezilla",
    "grocy",
    "calibre",
    "obsidian",
    "duplicati",
    "smokeping",
    "nzbget",
    "kavita",
    "mylar3",
    "deluge",
    "webtop",
    "swag",
    "lazylibrarian",
    "pyload-ng",
    "freshrss",
    "changedetection.io",
    "nzbhydra2",
    "mstream",
    "emby",
    "altus",
    "beets",
    "apprise-api",
    "faster-whisper",
    "nginx",
    "librespeed",
    "pairdrop",
    "ombi",
    "tvheadend",
    "piper",
    "resilio-sync",
    "adguardhome-sync",
    "chrome",
    "babybuddy",
    "healthchecks",
    "dokuwiki",
    "orcaslicer",
    "brave",
    "qdirstat",
    "inkscape",
    "libreoffice",
    "vscode",
    "thelounge",
    "piwigo",
    "ubooquity",
    "grav",
    "webcord",
    "diskover",
    "keepassxc",
    "projectsend",
    "manyfold",
    "thunderbird",
    "sqlitebrowser",
    "hishtory-server",
    "doublecommander",
    "airsonic-advanced",
    "telegram",
    "freecad",
    "remmina",
    "vscodium",
    "librewolf",
    "oscam",
    "medusa",
    "flexget",
    "foldingathome",
    "wireshark",
    "cops",
    "kali-linux",
    "ungoogled-chromium",
    "xbackbone",
    "retroarch",
    "helium",
    "blender",
    "kicad",
    "gimp",
    "pwndrop",
    "vscodium-web",
    "boinc",
    "lychee",
    "vivaldi",
    "ferdium",
]

DISPLAY_NAMES = {
    "adguardhome-sync": "AdGuard Home Sync",
    "airsonic-advanced": "Airsonic Advanced",
    "apprise-api": "Apprise API",
    "boinc": "BOINC",
    "changedetection.io": "ChangeDetection.io",
    "cops": "COPS",
    "dokuwiki": "DokuWiki",
    "freshrss": "FreshRSS",
    "github-desktop": "GitHub Desktop",
    "homeassistant": "Home Assistant",
    "keepassxc": "KeePassXC",
    "kicad": "KiCad",
    "libreoffice": "LibreOffice",
    "librespeed": "LibreSpeed",
    "mstream": "mStream",
    "mylar3": "Mylar3",
    "nginx": "NGINX",
    "nzbget": "NZBGet",
    "nzbhydra2": "NZBHydra2",
    "qbittorrent": "qBittorrent",
    "qdirstat": "QDirStat",
    "sabnzbd": "SABnzbd",
    "swag": "SWAG",
    "thelounge": "The Lounge",
    "tvheadend": "TVHeadend",
    "ubooquity": "Ubooquity",
    "vscode": "Visual Studio Code",
    "vscodium": "VSCodium",
    "vscodium-web": "VSCodium Web",
}

FRENCH_CATEGORY = {
    "3D Modeling": "création et modélisation 3D",
    "3D Printing": "impression 3D",
    "Administration": "administration de serveur",
    "Backup": "sauvegarde et synchronisation",
    "Books": "bibliothèque numérique",
    "Chat": "communication et messagerie",
    "Content Management": "gestion de contenu",
    "Dashboard": "tableau de bord",
    "Databases": "gestion de bases de données",
    "Documents": "bureautique et documents",
    "Downloaders": "gestion des téléchargements",
    "Email": "courrier électronique",
    "Family": "organisation familiale",
    "File Sharing": "partage de fichiers",
    "FTP": "transfert de fichiers",
    "Games": "jeux et émulation",
    "Home Automation": "domotique",
    "IRC": "communication IRC",
    "Image Editor": "retouche d’images",
    "Indexers": "indexation de contenus",
    "Machine Learning": "intelligence artificielle",
    "Media Management": "gestion de médias",
    "Media Requesters": "demandes de contenus multimédias",
    "Media Servers": "diffusion multimédia",
    "Media Tools": "outils multimédias",
    "Monitoring": "surveillance et diagnostic",
    "Music": "musique et audio",
    "Network": "réseau",
    "Password Manager": "gestion de mots de passe",
    "Photos": "photos et galeries",
    "Programming": "développement",
    "Recipes": "recettes et inventaire",
    "Remote Desktop": "bureau à distance",
    "Reverse Proxy": "publication de services web",
    "RSS": "lecture de flux RSS",
    "Science": "calcul scientifique",
    "Security": "sécurité",
    "Social": "réseaux sociaux",
    "Storage": "analyse du stockage",
    "Web Browser": "navigation web",
    "Web Tools": "automatisation web",
}

SKIPPED_VOLUME_TARGETS = {
    "/dev/input",
    "/lib/modules",
    "/opt/vc/lib",
    "/run/udev/data",
    "/var/run/docker.sock",
}


def fetch_catalog() -> dict[str, dict]:
    request = urllib.request.Request(API_URL, headers={"User-Agent": "MenhirOS-store-sync/1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    values = payload["data"]["repositories"]["linuxserver"]
    return {item["name"]: item for item in values}


def manifest_id(image_name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", image_name.lower()).strip("-")
    if value == "homeassistant":
        return "home-assistant"
    return value


def display_name(image_name: str) -> str:
    if image_name in DISPLAY_NAMES:
        return DISPLAY_NAMES[image_name]
    return " ".join(part.capitalize() for part in re.split(r"[-_.]+", image_name))


def clean_description(value: str) -> str:
    value = html.unescape(re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= 260:
        return value
    sentence = value[:257].rsplit(" ", 1)[0]
    return sentence + "…"


def french_description(item: dict) -> str:
    categories = [part.strip() for part in (item.get("category") or "").split(",")]
    subject = next((FRENCH_CATEGORY[part] for part in categories if part in FRENCH_CATEGORY), "services auto-hébergés")
    return f"Application auto-hébergée de {subject} pour votre serveur Menhir."


def resolve_digest(image_name: str) -> str:
    image = f"lscr.io/linuxserver/{image_name}:latest"
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", image, "--format", "{{json .Manifest}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    manifest = json.loads(result.stdout)
    architectures = {
        entry.get("platform", {}).get("architecture")
        for entry in manifest.get("manifests", [])
        if entry.get("platform", {}).get("os") == "linux"
    }
    missing = {"amd64", "arm64"} - architectures
    if missing:
        raise RuntimeError(f"{image} is missing Linux architectures: {', '.join(sorted(missing))}")
    digest = manifest.get("digest")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise RuntimeError(f"{image} did not return a valid OCI index digest")
    print(f"{image_name}: {digest}")
    return digest


def parse_ports(item: dict) -> list[dict]:
    ports: list[dict] = []
    seen: set[tuple[int, str]] = set()
    proxy_assigned = False
    for value in (item.get("config") or {}).get("ports") or []:
        raw = str(value.get("internal", ""))
        protocol = "udp" if raw.endswith("/udp") else "tcp"
        number_text = raw.split("/", 1)[0]
        if not number_text.isdigit():
            continue
        number = int(number_text)
        key = (number, protocol)
        if key in seen or number < 1 or number > 65535:
            continue
        seen.add(key)
        proxy = protocol == "tcp" and not proxy_assigned
        proxy_assigned = proxy_assigned or proxy
        ports.append({"container": number, "protocol": protocol, "proxy": proxy})
    if not proxy_assigned:
        raise RuntimeError(f"{item['name']} has no TCP port suitable for the Menhir web proxy")
    return ports


def volume_name(target: str, used: set[str]) -> str:
    candidate = re.sub(r"[^a-z0-9]+", "-", target.lower()).strip("-") or "data"
    original = candidate
    index = 2
    while candidate in used:
        candidate = f"{original}-{index}"
        index += 1
    used.add(candidate)
    return candidate


def parse_volumes(item: dict) -> list[dict]:
    volumes: list[dict] = []
    used: set[str] = set()
    for value in (item.get("config") or {}).get("volumes") or []:
        target = str(value.get("path", "")).split(":", 1)[0].rstrip("/")
        if not target.startswith("/") or target in SKIPPED_VOLUME_TARGETS:
            continue
        lowered = target.lower()
        if "cache" in lowered:
            volume_type = "cache"
        elif target == "/config" or lowered.startswith("/config/") or target in {"/opt", "/profiles"}:
            volume_type = "app"
        else:
            volume_type = "share"
        volumes.append({"name": volume_name(target, used), "target": target, "type": volume_type})
    return volumes


def resources(item: dict) -> dict:
    category = item.get("category") or ""
    if any(value in category for value in ("Machine Learning", "3D Modeling", "Remote Desktop", "Web Browser")):
        return {"memoryMinimum": "1G", "memoryRecommended": "2G", "cpuRecommended": 2}
    if any(value in category for value in ("Media Servers", "Photos", "Science", "Programming")):
        return {"memoryMinimum": "512M", "memoryRecommended": "1G", "cpuRecommended": 2}
    return {"memoryMinimum": "256M", "memoryRecommended": "512M", "cpuRecommended": 1}


def security_classification(item: dict) -> str:
    category = item.get("category") or ""
    if any(value in category for value in ("Password Manager", "Security", "Databases", "Email")):
        return "sensitive"
    return "standard"


def app_manifest(item: dict, digest: str) -> dict:
    image_name = item["name"]
    app_id = manifest_id(image_name)
    volumes = parse_volumes(item)
    container = {
        "name": app_id,
        "image": f"lscr.io/linuxserver/{image_name}:latest@{digest}",
        "ports": parse_ports(item),
    }
    if volumes:
        container["volumes"] = volumes
    backup_paths = [volume["target"] for volume in volumes if volume["type"] == "app"]
    name = display_name(image_name)
    return {
        "schemaVersion": "1",
        "id": app_id,
        "name": {"en": name, "fr": name},
        "description": {
            "en": clean_description(item.get("description") or f"Self-hosted {name} application."),
            "fr": french_description(item),
        },
        "version": str(item.get("version") or "latest"),
        "architectures": ["amd64", "arm64"],
        "containers": [container],
        "resources": resources(item),
        "secrets": [],
        "permissions": {
            "privileged": False,
            "hostNetwork": False,
            "capabilities": [],
            "devices": [],
        },
        "backup": {"mode": "crash-consistent", "paths": backup_paths},
        "migrations": [],
        "releaseNotes": {
            "en": f"Menhir integration for {name} {item.get('version') or 'latest'}.",
            "fr": f"Intégration Menhir pour {name} {item.get('version') or 'latest'}.",
        },
        "securityClassification": security_classification(item),
    }


def write_catalog(catalog: dict[str, dict], digests: dict[str, str]) -> None:
    apps_root = pathlib.Path("apps")
    references = []
    for image_name in APP_IDS:
        item = catalog[image_name]
        app_id = manifest_id(image_name)
        directory = apps_root / app_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "app.yaml"
        content = yaml.safe_dump(
            app_manifest(item, digests[image_name]),
            allow_unicode=True,
            sort_keys=False,
            width=1000,
        )
        path.write_text(content, encoding="utf-8")
        references.append({
            "id": app_id,
            "path": path.as_posix(),
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        })

    index_path = pathlib.Path("store.yaml")
    index = yaml.safe_load(index_path.read_text(encoding="utf-8"))
    preserved = [entry for entry in index["apps"] if entry["id"] not in {ref["id"] for ref in references}]
    index["apps"] = preserved + references
    index_path.write_text(
        yaml.safe_dump(index, allow_unicode=True, sort_keys=False, width=1000),
        encoding="utf-8",
    )


def main() -> int:
    if len(APP_IDS) != 100 or len(set(APP_IDS)) != 100:
        raise RuntimeError("APP_IDS must contain exactly 100 unique LinuxServer.io applications")
    catalog = fetch_catalog()
    missing = set(APP_IDS) - set(catalog)
    if missing:
        raise RuntimeError("LinuxServer.io catalog is missing: " + ", ".join(sorted(missing)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        resolved = pool.map(resolve_digest, APP_IDS)
        digests = dict(zip(APP_IDS, resolved, strict=True))
    write_catalog(catalog, digests)
    print(f"Wrote {len(APP_IDS)} applications; store now contains {len(yaml.safe_load(pathlib.Path('store.yaml').read_text())['apps'])} entries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
