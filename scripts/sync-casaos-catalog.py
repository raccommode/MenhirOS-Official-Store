#!/usr/bin/env python3
"""Import the complete official CasaOS/ZimaOS catalog into Menhir's store.

The converter intentionally keeps Menhir's already reviewed manifests when an
application already exists. New CasaOS applications are converted from Compose,
their images are pinned to immutable OCI digests, and the store index hashes are
updated. Re-running the script refreshes manifests previously imported by it.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html
import json
import pathlib
import posixpath
import re
import shlex
import subprocess
import time
import urllib.parse
import urllib.request
from collections.abc import Iterable

import yaml


CASA_REPOSITORY = "https://github.com/IceWhaleTech/CasaOS-AppStore"
LEGACY_IMPORT_MARKER = "Imported from the official CasaOS catalog"
IMPORT_STATE_PATH = pathlib.Path("scripts/catalog-imported-apps.txt")
SUPPORTED_ARCHITECTURES = {"amd64", "arm64"}
DEFAULT_ENVIRONMENT = {
    "PUID": "1000",
    "PGID": "1000",
    "TZ": "Etc/UTC",
}
APP_ID_ALIASES = {
    "homeassistant": "home-assistant",
    "pyload": "pyload-ng",
}
SENSITIVE_CATEGORY_WORDS = {
    "authentication",
    "database",
    "finance",
    "password",
    "security",
    "wallet",
}
SKIPPED_VOLUME_TARGETS = {
    "/dev",
    "/dev/dri",
    "/dev/input",
    "/lib/modules",
    "/proc",
    "/run",
    "/sys",
    "/var/run/docker.sock",
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=pathlib.Path, help="CasaOS-AppStore checkout")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--refresh-images", action="store_true")
    return parser.parse_args()


def slug(value: str) -> str:
    value = html.unescape(value).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return APP_ID_ALIASES.get(value, value)


def clean_text(value: object, fallback: str, limit: int = 520) -> str:
    text = str(value or "")
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[*_`#>|]+", " ", text)
    text = html.unescape(re.sub(r"\s+", " ", text)).strip() or fallback
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"


def locale_value(values: object, language: str, fallback: str) -> str:
    if not isinstance(values, dict):
        return fallback
    choices = ("fr_FR", "fr") if language == "fr" else ("en_US", "en_GB", "en")
    for key in choices:
        value = values.get(key)
        if value:
            return clean_text(value, fallback)
    return fallback


def command(value: object) -> list[str]:
    if value is None or value == [] or value == "":
        return []
    if isinstance(value, list):
        return [scalar(item) for item in value]
    return shlex.split(str(value))


def scalar(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def environment_pairs(value: object) -> list[tuple[str, str]]:
    if isinstance(value, dict):
        return [(str(key), scalar(item)) for key, item in value.items()]
    pairs: list[tuple[str, str]] = []
    if isinstance(value, list):
        for item in value:
            key, separator, raw = str(item).partition("=")
            pairs.append((key, raw if separator else ""))
    return pairs


VARIABLE = re.compile(
    r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(?:(?::-|-)([^}]*))?\}|([A-Za-z_][A-Za-z0-9_]*))"
)
SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{32,}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"AIza[A-Za-z0-9_-]{30,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(r"AKIA[A-Z0-9]{16}"),
)
SECRET_KEY_PATTERN = re.compile(
    r"(?:^|_)(?:PASSWORD|PASSWD|SECRET|SECRETKEY|TOKEN|APIKEY|API_KEY|"
    r"PRIVATE_KEY|ENCRYPTION_KEY|CREDENTIALS?|CREDS_KEY|MASTER_KEY|ACCESS_KEY|IV)(?:_|$)"
)
NON_SECRET_KEY_PATTERNS = (
    re.compile(r"^ALLOW_"),
    re.compile(r"(?:_PATH|_FILE|_URL|_URI|_EXPIRY|_EXPIRES|_TTL|_LENGTH|_ENABLED|_USERS|_RESET)$"),
)
USER_PROVIDED_SECRET_PATTERNS = (
    re.compile(
        r"^(?:OPENAI|ANTHROPIC|ASSISTANTS|GOOGLE|AWS|AZURE|GITHUB|GITLAB|"
        r"SLACK|DISCORD|TELEGRAM|COHERE|GROQ|MISTRAL|TOGETHER|PERPLEXITY|"
        r"HUGGINGFACE|HF|SMTP|OAUTH)_"
    ),
    re.compile(r"(?:^|_)(?:CLIENT_SECRET|BOT_TOKEN|WEBHOOK_SECRET)$"),
)


def expand_compose_value(
    value: str,
    app_id: str,
    secrets: dict[str, dict],
) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(3) or ""
        default = match.group(2)
        if name in DEFAULT_ENVIRONMENT:
            return DEFAULT_ENVIRONMENT[name]
        if name.lower() == "appid":
            return app_id
        if name == "HOSTNAME":
            return app_id
        if default is not None:
            return default
        secrets.setdefault(
            name,
            {
                "name": name,
                "label": {"en": name.replace("_", " ").title(), "fr": name.replace("_", " ").title()},
                "description": {
                    "en": f"Optional value used by {app_id}.",
                    "fr": f"Valeur facultative utilisée par {app_id}.",
                },
                "required": False,
                "generate": False,
            },
        )
        return "${secret:" + name + "}"

    return VARIABLE.sub(replace, value).replace("$$", "$")


def parse_environment(service: dict, app_id: str, secrets: dict[str, dict]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in environment_pairs(service.get("environment") or {}):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        expanded = expand_compose_value(value.strip(), app_id, secrets)
        if "\n" not in expanded and "\r" not in expanded and "\x00" not in expanded:
            result[key] = expanded
    return result


def protect_detected_secrets(
    containers: list[dict],
    secrets: dict[str, dict],
    app_id: str,
) -> None:
    protected_values: dict[str, str] = {}

    def secret_token(
        base_name: str,
        *,
        generate: bool,
        required: bool,
    ) -> str:
        name = base_name
        suffix = 2
        while name in secrets and (
            secrets[name].get("generate") != generate
            or secrets[name].get("required") != required
        ):
            name = f"{base_name}_{suffix}"
            suffix += 1
        secrets[name] = {
            "name": name,
            "label": {
                "en": base_name.replace("_", " ").title(),
                "fr": base_name.replace("_", " ").title(),
            },
            "description": {
                "en": (
                    f"Secret generated securely for {app_id}."
                    if generate
                    else f"Credential supplied during installation of {app_id}."
                ),
                "fr": (
                    f"Secret généré de façon sécurisée pour {app_id}."
                    if generate
                    else f"Identifiant fourni pendant l’installation de {app_id}."
                ),
            },
            "required": required,
            "generate": generate,
        }
        return "${secret:" + name + "}"

    generated_groups: dict[str, list[str]] = {}
    for container in containers:
        environment = container.get("environment") or {}
        for key, value in list(environment.items()):
            if value.startswith("${secret:"):
                continue
            upper_key = key.upper()
            known_secret_value = any(
                pattern.fullmatch(value) for pattern in SECRET_VALUE_PATTERNS
            )
            secret_key = SECRET_KEY_PATTERN.search(upper_key) is not None
            if secret_key and any(
                pattern.search(upper_key) for pattern in NON_SECRET_KEY_PATTERNS
            ):
                secret_key = False
            if not secret_key and not known_secret_value:
                continue
            if not value or "\n" in value or "\r" in value or "\x00" in value:
                continue
            user_provided = upper_key == "DEFAULT_ADMIN_PASSWORD" or any(
                pattern.search(upper_key)
                for pattern in USER_PROVIDED_SECRET_PATTERNS
            )
            if user_provided:
                environment[key] = secret_token(
                    key,
                    generate=False,
                    required=upper_key == "DEFAULT_ADMIN_PASSWORD",
                )
                continue
            generated_groups.setdefault(value, []).append(key)

    for value, keys in generated_groups.items():
        protected_values[value] = secret_token(
            keys[0],
            generate=True,
            required=True,
        )

    def replace_protected(value: str) -> str:
        for protected, token in sorted(
            protected_values.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            value = value.replace(protected, token)
        return value

    for container in containers:
        environment = container.get("environment") or {}
        for key, value in environment.items():
            environment[key] = replace_protected(value)
        if container.get("command"):
            container["command"] = [
                replace_protected(value) for value in container["command"]
            ]
        healthcheck = container.get("healthcheck") or {}
        if healthcheck.get("test"):
            healthcheck["test"] = [
                replace_protected(value) for value in healthcheck["test"]
            ]


def parse_port(value: object) -> tuple[int, str, str] | None:
    if isinstance(value, dict):
        target = value.get("target")
        protocol = str(value.get("protocol") or "tcp").lower()
        published = scalar(value.get("published"))
    else:
        raw = str(value)
        protocol = "udp" if raw.endswith("/udp") else "tcp"
        raw = re.sub(r"/(?:tcp|udp)$", "", raw)
        parts = raw.rsplit(":", 2)
        target = parts[-1]
        published = parts[-2] if len(parts) > 1 else ""
    number = str(target).strip().strip('"')
    if not number.isdigit() or protocol not in {"tcp", "udp"}:
        return None
    port = int(number)
    if port < 1 or port > 65535:
        return None
    return port, protocol, published


def parse_ports(service: dict) -> list[dict]:
    result: list[dict] = []
    seen: set[tuple[int, str]] = set()
    for raw in [*(service.get("ports") or []), *(service.get("expose") or [])]:
        parsed = parse_port(raw)
        if parsed is None:
            continue
        port, protocol, _ = parsed
        key = (port, protocol)
        if key not in seen:
            seen.add(key)
            result.append({"container": port, "protocol": protocol, "proxy": False})
    return result


def volume_parts(value: object) -> tuple[str, str] | None:
    if isinstance(value, dict):
        source = scalar(value.get("source"))
        target = scalar(value.get("target"))
    else:
        raw = str(value)
        parts = raw.split(":")
        if len(parts) == 1:
            source, target = "", parts[0]
        else:
            source, target = parts[0], parts[1]
    target = posixpath.normpath(target.rstrip("/"))
    if not target.startswith("/") or target == "/" or target in SKIPPED_VOLUME_TARGETS:
        return None
    return source, target


def unique_name(candidate: str, used: set[str]) -> str:
    candidate = slug(candidate) or "data"
    base = candidate
    index = 2
    while candidate in used:
        candidate = f"{base}-{index}"
        index += 1
    used.add(candidate)
    return candidate


def parse_volumes(service: dict) -> list[dict]:
    result: list[dict] = []
    used_names: set[str] = set()
    used_targets: set[str] = set()
    for raw in service.get("volumes") or []:
        parsed = volume_parts(raw)
        if parsed is None:
            continue
        source, target = parsed
        # Menhir manifests intentionally cannot bind arbitrary host paths.
        # CasaOS system mounts (Docker socket, /etc/localtime, /proc, etc.)
        # therefore remain explicit permissions to be implemented separately
        # instead of silently shadowing those files with empty app directories.
        if source.startswith("/") and not source.startswith("/DATA/"):
            continue
        if target in used_targets:
            continue
        used_targets.add(target)
        lowered = (source + " " + target).lower()
        if "cache" in lowered:
            volume_type = "cache"
        elif source.startswith("/DATA/") and "/AppData/" not in source:
            volume_type = "share"
        else:
            volume_type = "app"
        source_name = pathlib.PurePosixPath(source.replace("$AppID", "").rstrip("/")).name
        name = unique_name(source_name or pathlib.PurePosixPath(target).name, used_names)
        result.append({"name": name, "target": target, "type": volume_type})
    return result


def parse_healthcheck(service: dict) -> dict | None:
    value = service.get("healthcheck")
    if not isinstance(value, dict):
        return None
    raw_test = value.get("test")
    if not raw_test or raw_test == ["NONE"]:
        return None
    if isinstance(raw_test, list):
        test = [scalar(item) for item in raw_test]
    else:
        test = ["CMD-SHELL", scalar(raw_test)]
    return {
        "test": test,
        "interval": scalar(value.get("interval") or "30s"),
        "timeout": scalar(value.get("timeout") or "10s"),
        "retries": int(value.get("retries") or 3),
    }


def topological_services(services: dict[str, dict]) -> list[tuple[str, dict]]:
    enabled = {name: value for name, value in services.items() if not value.get("profiles")}
    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited or name not in enabled:
            return
        if name in visiting:
            return
        visiting.add(name)
        dependencies = enabled[name].get("depends_on") or []
        if isinstance(dependencies, dict):
            dependencies = dependencies.keys()
        for dependency in dependencies:
            visit(str(dependency))
        visiting.remove(name)
        visited.add(name)
        ordered.append(name)

    for name in enabled:
        visit(name)
    return [(name, enabled[name]) for name in ordered]


def memory_megabytes(value: object) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMG]?)B?\s*", scalar(value), re.I)
    if not match:
        return 0
    amount = float(match.group(1))
    unit = match.group(2).upper()
    multiplier = {"": 1 / (1024 * 1024), "K": 1 / 1024, "M": 1, "G": 1024}[unit]
    return max(1, int(amount * multiplier))


def format_memory(megabytes: int) -> str:
    if megabytes >= 1024 and megabytes % 1024 == 0:
        return f"{megabytes // 1024}G"
    return f"{megabytes}M"


def resource_guidance(services: Iterable[dict], category: str) -> dict:
    services = list(services)
    reserved = 0
    for service in services:
        deploy = service.get("deploy") or {}
        resources = deploy.get("resources") or {}
        reservation = resources.get("reservations") or {}
        reserved += memory_megabytes(reservation.get("memory"))
    minimum = max(256, reserved)
    if any(word in category.lower() for word in ("ai", "machine", "video", "virtual")):
        minimum = max(minimum, 1024)
    recommended = max(512, minimum * 2)
    cpu = 2 if len(services) > 1 or minimum >= 1024 else 1
    return {
        "memoryMinimum": format_memory(minimum),
        "memoryRecommended": format_memory(recommended),
        "cpuRecommended": cpu,
    }


def device_paths(service: dict) -> list[str]:
    result: list[str] = []
    for raw in service.get("devices") or []:
        source = scalar(raw.get("source")) if isinstance(raw, dict) else str(raw).split(":", 1)[0]
        if source.startswith("/") and source not in result:
            result.append(source)
    return result


def compose_architectures(meta: dict) -> set[str]:
    return set(meta.get("architectures") or []) & SUPPORTED_ARCHITECTURES


def docker_hub_coordinates(reference: str) -> tuple[str, str] | None:
    base = reference.split("@", 1)[0]
    first = base.split("/", 1)[0]
    if "/" in base and ("." in first or ":" in first or first == "localhost"):
        if first not in {"docker.io", "index.docker.io", "registry-1.docker.io"}:
            return None
        base = base.split("/", 1)[1]
    if "/" not in base:
        base = "library/" + base
    last_slash = base.rfind("/")
    last_colon = base.rfind(":")
    if last_colon > last_slash:
        repository, tag = base[:last_colon], base[last_colon + 1 :]
    else:
        repository, tag = base, "latest"
    return repository, tag


def inspect_docker_hub_image(reference: str) -> dict | None:
    coordinates = docker_hub_coordinates(reference)
    if coordinates is None:
        return None
    repository, tag = coordinates
    url = (
        "https://hub.docker.com/v2/repositories/"
        + "/".join(urllib.parse.quote(part, safe="") for part in repository.split("/"))
        + "/tags/"
        + urllib.parse.quote(tag, safe="")
    )
    request = urllib.request.Request(url, headers={"User-Agent": "MenhirOS-store-sync/1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    pinned = re.search(r"@(sha256:[a-f0-9]{64})$", reference)
    digest = pinned.group(1) if pinned else payload.get("digest")
    architectures = {
        image.get("architecture")
        for image in payload.get("images") or []
        if image.get("os") == "linux"
    } & SUPPORTED_ARCHITECTURES
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        return None
    if not architectures:
        return None
    print(f"{reference}: {digest} ({', '.join(sorted(architectures))})", flush=True)
    return {"digest": digest, "architectures": sorted(architectures)}


def inspect_image(reference: str) -> dict:
    error: Exception | None = None
    for attempt in range(4):
        try:
            docker_hub = inspect_docker_hub_image(reference)
            if docker_hub is not None:
                return docker_hub
            result = subprocess.run(
                [
                    "docker",
                    "buildx",
                    "imagetools",
                    "inspect",
                    reference,
                    "--format",
                    "{{json .Manifest}}",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )
            manifest = json.loads(result.stdout)
            digest = manifest.get("digest")
            architectures = {
                item.get("platform", {}).get("architecture")
                for item in manifest.get("manifests", [])
                if item.get("platform", {}).get("os") == "linux"
            }
            if not architectures:
                image_result = subprocess.run(
                    [
                        "docker",
                        "buildx",
                        "imagetools",
                        "inspect",
                        reference,
                        "--format",
                        "{{json .Image}}",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                image = json.loads(image_result.stdout)
                if isinstance(image, dict) and image.get("architecture"):
                    architectures.add(image["architecture"])
                elif isinstance(image, dict):
                    architectures.update(
                        key.split("/", 1)[1]
                        for key in image
                        if isinstance(key, str) and key.startswith("linux/")
                    )
            if not isinstance(digest, str):
                digest_match = re.search(r"@?(sha256:[a-f0-9]{64})", reference)
                if digest_match:
                    digest = digest_match.group(1)
            if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
                raise RuntimeError(f"{reference} did not expose a valid OCI digest")
            architectures &= SUPPORTED_ARCHITECTURES
            if not architectures:
                raise RuntimeError(f"{reference} did not expose a supported Linux architecture")
            print(f"{reference}: {digest} ({', '.join(sorted(architectures))})", flush=True)
            return {"digest": digest, "architectures": sorted(architectures)}
        except (
            json.JSONDecodeError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            RuntimeError,
        ) as reason:
            error = reason
            if attempt < 3:
                time.sleep(2**attempt)
    assert error is not None
    raise RuntimeError(f"cannot resolve {reference}: {error}") from error


def image_cache(path: pathlib.Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    cache = json.loads(path.read_text(encoding="utf-8"))
    for reference, resolution in cache.items():
        pinned = re.search(r"@(sha256:[a-f0-9]{64})$", reference)
        if pinned:
            resolution["digest"] = pinned.group(1)
    return cache


def resolve_images(
    references: set[str],
    cache_path: pathlib.Path,
    workers: int,
    refresh: bool,
) -> dict[str, dict]:
    cache = image_cache(cache_path)
    pending = sorted(references if refresh else references - cache.keys())
    errors: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(inspect_image, reference): reference for reference in pending}
        for future in concurrent.futures.as_completed(futures):
            reference = futures[future]
            try:
                cache[reference] = future.result()
            except RuntimeError as error:
                errors.append(str(error))
                continue
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if errors:
        raise RuntimeError("image resolution failed:\n- " + "\n- ".join(sorted(errors)))
    return {reference: cache[reference] for reference in references}


def pin_image(reference: str, resolution: dict) -> str:
    base = reference.split("@", 1)[0]
    return f"{base}@{resolution['digest']}"


def application_identity(path: pathlib.Path, compose: dict) -> tuple[str, str]:
    meta = compose.get("x-casaos") or {}
    name = locale_value(meta.get("title"), "en", path.parent.name)
    app_id = slug(name)
    if not app_id:
        raise RuntimeError(f"{path} has no usable application id")
    return app_id, name


def previously_imported(path: pathlib.Path) -> bool:
    if not path.exists():
        return False
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    return LEGACY_IMPORT_MARKER in (manifest.get("releaseNotes") or {}).get("en", "")


def imported_applications(
    source: pathlib.Path,
    existing_ids: set[str],
    imported_ids: set[str],
) -> list[tuple[pathlib.Path, dict]]:
    result: list[tuple[pathlib.Path, dict]] = []
    for path in sorted((source / "Apps").glob("*/docker-compose.yml")):
        compose = yaml.safe_load(path.read_text(encoding="utf-8"))
        app_id, _ = application_identity(path, compose)
        destination = pathlib.Path("apps") / app_id / "app.yaml"
        if (
            app_id in existing_ids
            and app_id not in imported_ids
            and not previously_imported(destination)
        ):
            continue
        result.append((path, compose))
    return result


def neutralize_visible_catalog_branding(value: str) -> str:
    value = re.sub(
        r"Imported from the official CasaOS catalog(?: at revision [^.]+)?\.\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"Importation depuis le catalogue officiel CasaOS(?: à la révision [^.]+)?\.\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"from the official CasaOS catalog",
        "ready to install on Menhir OS",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"issue du catalogue officiel CasaOS",
        "prête à installer sur Menhir OS",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\b(?:CasaOS|ZimaOS)\b", "Menhir OS", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip()
    return value.lstrip("- ").strip()


def sanitize_visible_manifest_fields(path: pathlib.Path) -> str:
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    for field in ("name", "description", "releaseNotes"):
        localized = manifest.get(field) or {}
        for language, value in localized.items():
            sanitized = neutralize_visible_catalog_branding(str(value))
            if not sanitized and field == "releaseNotes":
                sanitized = (
                    "Manifeste synchronisé avec les métadonnées amont vérifiées."
                    if language == "fr"
                    else "Manifest synchronized with verified upstream metadata."
                )
            localized[language] = sanitized
    content = yaml.safe_dump(
        manifest,
        allow_unicode=True,
        sort_keys=False,
        width=1000,
    )
    path.write_text(content, encoding="utf-8")
    return hashlib.sha256(content.encode()).hexdigest()


def app_manifest(
    path: pathlib.Path,
    compose: dict,
    images: dict[str, dict],
    source_commit: str,
) -> dict:
    meta = compose.get("x-casaos") or {}
    app_id, name = application_identity(path, compose)
    secrets: dict[str, dict] = {}
    source_services = topological_services(compose.get("services") or {})
    if not source_services:
        raise RuntimeError(f"{path} has no enabled services")
    main_service = str(meta.get("main") or source_services[-1][0])
    host_network = any(service.get("network_mode") == "host" for _, service in source_services)
    containers: list[dict] = []
    application_architectures = compose_architectures(meta) or set(SUPPORTED_ARCHITECTURES)

    for service_name, service in source_services:
        reference = scalar(service.get("image"))
        if not reference:
            raise RuntimeError(f"{path}: service {service_name} has no image")
        application_architectures &= set(images[reference]["architectures"])
        container: dict = {
            "name": service_name,
            "image": pin_image(reference, images[reference]),
        }
        parsed_command = command(service.get("command"))
        if parsed_command:
            container["command"] = parsed_command
        environment = parse_environment(service, app_id, secrets)
        if environment:
            container["environment"] = environment
        ports = parse_ports(service)
        if ports:
            container["ports"] = ports
        volumes = parse_volumes(service)
        if volumes:
            container["volumes"] = volumes
        healthcheck = parse_healthcheck(service)
        if healthcheck:
            container["healthcheck"] = healthcheck
        containers.append(container)

    protect_detected_secrets(containers, secrets, app_id)

    if not application_architectures:
        raise RuntimeError(f"{path}: no architecture is supported by every container")

    if not host_network:
        proxy_container = next((item for item in containers if item["name"] == main_service), None)
        candidates = [proxy_container] if proxy_container else []
        candidates += [item for item in reversed(containers) if item is not proxy_container]
        for candidate in candidates:
            if candidate is None:
                continue
            proxy_port = next(
                (
                    port
                    for port in candidate.get("ports") or []
                    if port["protocol"] == "tcp"
                ),
                None,
            )
            if proxy_port:
                proxy_port["proxy"] = True
                break

    category = scalar(meta.get("category") or "Other")
    fallback_description = f"Self-hosted {name} application ready to install on Menhir OS."
    description_en = locale_value(meta.get("description"), "en", fallback_description)
    description_fr = locale_value(
        meta.get("description"),
        "fr",
        f"Application {name} auto-hébergée prête à installer sur Menhir OS.",
    )
    release_en = locale_value(
        meta.get("release_notes"),
        "en",
        f"Manifest synchronized with verified upstream metadata at revision {source_commit[:12]}.",
    )
    release_fr = locale_value(
        meta.get("release_notes"),
        "fr",
        f"Manifeste synchronisé avec les métadonnées amont vérifiées à la révision {source_commit[:12]}.",
    )
    description_en = neutralize_visible_catalog_branding(description_en)
    description_fr = neutralize_visible_catalog_branding(description_fr)
    release_en = neutralize_visible_catalog_branding(release_en)
    release_fr = neutralize_visible_catalog_branding(release_fr)
    icon = scalar(meta.get("icon")).replace("http://", "https://", 1)
    if not icon.startswith("https://"):
        raise RuntimeError(f"{path}: application icon must use HTTPS")

    all_volumes = [volume for container in containers for volume in container.get("volumes") or []]
    backup_paths = sorted(
        {
            volume["target"]
            for volume in all_volumes
            if volume["type"] == "app"
        }
    )
    privileged = any(bool(service.get("privileged")) for _, service in source_services)
    capabilities = sorted(
        {
            scalar(capability)
            for _, service in source_services
            for capability in service.get("cap_add") or []
        }
    )
    devices = sorted(
        {
            device
            for _, service in source_services
            for device in device_paths(service)
        }
    )
    sensitive = any(word in category.lower() for word in SENSITIVE_CATEGORY_WORDS)
    security = "critical" if privileged or host_network or capabilities or devices else ("sensitive" if sensitive else "standard")
    return {
        "schemaVersion": "1",
        "id": app_id,
        "name": {"en": name, "fr": name},
        "description": {"en": description_en, "fr": description_fr},
        "icon": icon,
        "version": scalar(meta.get("version") or "latest"),
        "architectures": sorted(application_architectures),
        "containers": containers,
        "resources": resource_guidance((service for _, service in source_services), category),
        "secrets": list(secrets.values()),
        "permissions": {
            "privileged": privileged,
            "hostNetwork": host_network,
            "capabilities": capabilities,
            "devices": devices,
        },
        "backup": {"mode": "crash-consistent", "paths": backup_paths},
        "migrations": [],
        "releaseNotes": {"en": release_en, "fr": release_fr},
        "securityClassification": security,
    }


def write_manifest(app: dict) -> dict[str, str]:
    path = pathlib.Path("apps") / app["id"] / "app.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = yaml.safe_dump(app, allow_unicode=True, sort_keys=False, width=1000)
    path.write_text(content, encoding="utf-8")
    return {
        "id": app["id"],
        "path": path.as_posix(),
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
    }


def git_revision(source: pathlib.Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def main() -> int:
    options = arguments()
    source = options.source.resolve()
    if not (source / "Apps").is_dir():
        raise RuntimeError(f"{source} is not a CasaOS-AppStore checkout")
    index_path = pathlib.Path("store.yaml")
    index = yaml.safe_load(index_path.read_text(encoding="utf-8"))
    existing_ids = {entry["id"] for entry in index["apps"]}
    imported_ids = (
        {
            line.strip()
            for line in IMPORT_STATE_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        if IMPORT_STATE_PATH.exists()
        else set()
    )
    selected = imported_applications(source, existing_ids, imported_ids)
    references = {
        scalar(service.get("image"))
        for _, compose in selected
        for service in (compose.get("services") or {}).values()
        if service.get("image") and not service.get("profiles")
    }
    images = resolve_images(
        references,
        pathlib.Path(".cache/casaos-images.json"),
        options.workers,
        options.refresh_images,
    )
    source_commit = git_revision(source)
    generated = [
        write_manifest(app_manifest(path, compose, images, source_commit))
        for path, compose in selected
    ]
    generated_ids = {entry["id"] for entry in generated}
    preserved = [entry for entry in index["apps"] if entry["id"] not in generated_ids]
    index["apps"] = preserved + sorted(generated, key=lambda entry: entry["id"])
    for entry in index["apps"]:
        entry["sha256"] = sanitize_visible_manifest_fields(pathlib.Path(entry["path"]))
    IMPORT_STATE_PATH.write_text(
        "\n".join(sorted(generated_ids)) + "\n",
        encoding="utf-8",
    )
    index_path.write_text(
        yaml.safe_dump(index, allow_unicode=True, sort_keys=False, width=1000),
        encoding="utf-8",
    )
    official_count = len(list((source / "Apps").glob("*/docker-compose.yml")))
    print(
        f"Imported {len(generated)} CasaOS applications from {source_commit}; "
        f"{official_count - len(generated)} already-reviewed applications were preserved. "
        f"The Menhir store now contains {len(index['apps'])} applications."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
