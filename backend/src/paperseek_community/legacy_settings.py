from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
import tempfile


LEGACY_SETTING_ALLOWLIST = {
    "LLM_API_KEY": "PAPERSEEK_LLM_API_KEY",
    "LLM_API_TYPE": "PAPERSEEK_LLM_API_TYPE",
    "LLM_BASE_URL": "PAPERSEEK_LLM_BASE_URL",
    "LLM_MODEL": "PAPERSEEK_LLM_MODEL",
    "OPENALEX_API_KEY": "PAPERSEEK_OPENALEX_API_KEY",
    "PUBMED_API_KEY": "PAPERSEEK_PUBMED_API_KEY",
    "SEMANTIC_SCHOLAR_API_KEY": "PAPERSEEK_SEMANTIC_SCHOLAR_API_KEY",
    "SERPER_API_KEY": "PAPERSEEK_GOOGLE_SCHOLAR_API_KEY",
    "SERPER_API_KEYS": "PAPERSEEK_GOOGLE_SCHOLAR_API_KEY",
    "WOS_API_KEY": "PAPERSEEK_WOS_API_KEY",
    "WOS_DB": "PAPERSEEK_WOS_DB",
}

CURRENT_SETTING_NAMES = (
    "PAPERSEEK_DATABASE",
    "PAPERSEEK_LLM_API_TYPE",
    "PAPERSEEK_LLM_BASE_URL",
    "PAPERSEEK_LLM_MODEL",
    "PAPERSEEK_LLM_API_KEY",
    "PAPERSEEK_OPENALEX_API_KEY",
    "PAPERSEEK_SEMANTIC_SCHOLAR_API_KEY",
    "PAPERSEEK_PUBMED_API_KEY",
    "PAPERSEEK_WOS_API_KEY",
    "PAPERSEEK_WOS_DB",
    "PAPERSEEK_GOOGLE_SCHOLAR_API_KEY",
    "PAPERSEEK_WANFANG_API_KEY",
    "PAPERSEEK_CONTACT_EMAIL",
    "PAPERSEEK_PROXY_URLS",
    "PAPERSEEK_PROXY_SCOPE",
    "PAPERSEEK_MAX_CONCURRENT_RUNS",
)

# Docker Compose reads these from the same .env file; the application does not, but they stay in place.
COMPOSE_SETTING_NAMES = ("PAPERSEEK_VERSION", "PAPERSEEK_HOST", "PAPERSEEK_PORT")

_SETTING_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class LegacySettingsMigrationError(ValueError):
    def __init__(self, code: str, settings: tuple[str, ...] = ()) -> None:
        super().__init__(f"Legacy settings migration failed ({code}).")
        self.code = code
        self.settings = settings


def classify_legacy_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, list[str]]:
    values = environment if environment is not None else os.environ
    mappable = sorted(
        key for key in LEGACY_SETTING_ALLOWLIST if str(values.get(key) or "")
    )
    return {"mappable_settings": mappable}


def migrate_legacy_env(source: Path | str, target: Path | str) -> dict[str, object]:
    source_path = Path(source).expanduser().resolve()
    target_path = Path(target).expanduser().resolve()
    if source_path == target_path:
        raise LegacySettingsMigrationError("legacy_env_target_must_differ")
    source_values = _read_env(source_path, missing_code="legacy_env_not_found")
    migrated = {
        key: value
        for key, value in source_values.items()
        if key in CURRENT_SETTING_NAMES
    }
    mapped_settings = sorted(
        key for key, value in source_values.items() if value and key in LEGACY_SETTING_ALLOWLIST
    )
    shadowed_settings: list[str] = []
    for legacy_name in mapped_settings:
        if legacy_name == "SERPER_API_KEY" and source_values.get("SERPER_API_KEYS"):
            shadowed_settings.append(legacy_name)
            continue
        migrated[LEGACY_SETTING_ALLOWLIST[legacy_name]] = source_values[legacy_name]

    existing = (
        _read_env(target_path, missing_code="target_env_not_found")
        if target_path.exists()
        else {}
    )
    unsupported_target = tuple(sorted(
        key for key in existing if key not in CURRENT_SETTING_NAMES and key not in COMPOSE_SETTING_NAMES))
    if unsupported_target:
        raise LegacySettingsMigrationError("unsupported_target_setting", unsupported_target)
    conflicts = tuple(
        sorted(
            key
            for key, value in migrated.items()
            if key in existing and existing[key] != value
        )
    )
    if conflicts:
        raise LegacySettingsMigrationError("target_setting_conflict", conflicts)

    combined = {**migrated, **existing}
    rendered = _render_env(combined)
    previous = target_path.read_text(encoding="utf-8") if target_path.exists() else None
    status = "already_migrated" if previous == rendered else "migrated"
    if status == "migrated":
        _write_atomic(target_path, rendered)
    ignored = sorted(
        key
        for key in source_values
        if key not in CURRENT_SETTING_NAMES
        and key not in LEGACY_SETTING_ALLOWLIST
    )
    return {
        "status": status,
        "source": str(source_path),
        "target": str(target_path),
        "mapped_settings": mapped_settings,
        "current_settings": sorted(key for key in source_values if key in CURRENT_SETTING_NAMES),
        "shadowed_settings": shadowed_settings,
        "ignored_settings": ignored,
    }


def _read_env(path: Path, *, missing_code: str) -> dict[str, str]:
    if not path.is_file():
        raise LegacySettingsMigrationError(missing_code)
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise LegacySettingsMigrationError("legacy_env_invalid") from exc
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise LegacySettingsMigrationError("legacy_env_invalid")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not _SETTING_NAME.fullmatch(key):
            raise LegacySettingsMigrationError("legacy_env_invalid")
        values[key] = _decode_value(raw_value.strip())
    return values


def _decode_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise LegacySettingsMigrationError("legacy_env_invalid") from exc
        return str(decoded)
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


def _render_env(values: Mapping[str, str]) -> str:
    lines = [
        "# PaperSeek v0.3 deployment settings generated by `paperseek migrate`.",
        "# Review this file, then remove the legacy source file after a successful startup.",
    ]
    for key in (*COMPOSE_SETTING_NAMES, *CURRENT_SETTING_NAMES):
        if key in values:
            lines.append(f"{key}={json.dumps(values[key], ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def _write_atomic(path: Path, content: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        try:
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError as exc:
        raise LegacySettingsMigrationError("legacy_env_write_failed") from exc


__all__ = [
    "CURRENT_SETTING_NAMES",
    "LEGACY_SETTING_ALLOWLIST",
    "LegacySettingsMigrationError",
    "classify_legacy_environment",
    "migrate_legacy_env",
]
