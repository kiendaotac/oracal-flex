"""Configuration loading for the A1.Flex retry launcher.

Precedence (highest first):
    1. Real process environment variables (incl. `docker run -e`)
    2. Variables from the .env file
    3. Values from config.yaml
    4. Built-in defaults

Loading is deliberately lenient: missing or malformed values are recorded in an
error list instead of raising, so that `--check` can report every problem in one
pass instead of dying on the first one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

# --------------------------------------------------------------------------- #
# Always Free A1 (Ampere) tenancy-wide limits.
# --------------------------------------------------------------------------- #
FREE_TIER_MAX_OCPUS: Final[float] = 4.0
FREE_TIER_MAX_MEMORY_GB: Final[float] = 24.0
FREE_TIER_MAX_GB_PER_OCPU: Final[float] = 6.0
A1_SHAPE: Final[str] = "VM.Standard.A1.Flex"

DEFAULTS: Final[dict[str, str]] = {
    "SHAPE": A1_SHAPE,
    "DISPLAY_NAME": "a1-flex",
    "OCPUS": "2",
    "MEMORY_GB": "12",
    "BOOT_VOLUME_GB": "50",
    "ASSIGN_PUBLIC_IP": "true",
    "SSH_PUBLIC_KEY_PATH": "/keys/id_ed25519.pub",
    "OCI_CONFIG_FILE": "~/.oci/config",
    "OCI_PROFILE": "DEFAULT",
    "AVAILABILITY_DOMAINS": "",          # empty = auto-discover all ADs
    "FAULT_DOMAIN": "",
    "NSG_IDS": "",
    "MIN_DELAY_SECONDS": "60",
    "MAX_DELAY_SECONDS": "180",
    "AD_DELAY_SECONDS": "10",
    "RATE_LIMIT_SLEEP_SECONDS": "900",
    "MAX_ROUNDS": "0",                   # 0 = retry forever
    "LOG_FILE": "/var/log/a1launcher/a1launcher.log",
    "LOG_LEVEL": "INFO",
    "LOG_MAX_BYTES": "5242880",
    "LOG_BACKUP_COUNT": "3",
    "TELEGRAM_ENABLED": "false",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
}

REQUIRED_KEYS: Final[tuple[str, ...]] = (
    "COMPARTMENT_ID",
    "SUBNET_ID",
    "IMAGE_ID",
    "SSH_PUBLIC_KEY_PATH",
    "DISPLAY_NAME",
)

_TRUE_VALUES: Final[frozenset[str]] = frozenset({"1", "true", "yes", "y", "on"})
_FALSE_VALUES: Final[frozenset[str]] = frozenset({"0", "false", "no", "n", "off"})


@dataclass(slots=True)
class AppConfig:
    """Fully resolved runtime configuration."""

    # --- OCI identity / placement -----------------------------------------
    compartment_id: str
    subnet_id: str
    image_id: str
    oci_config_file: Path
    oci_profile: str

    # --- Instance shape ----------------------------------------------------
    shape: str
    display_name: str
    ocpus: float
    memory_gb: float
    boot_volume_gb: int
    ssh_public_key_path: Path
    assign_public_ip: bool
    availability_domains: list[str]
    fault_domain: str | None
    nsg_ids: list[str]

    # --- Retry behaviour ---------------------------------------------------
    min_delay_seconds: float
    max_delay_seconds: float
    ad_delay_seconds: float
    rate_limit_sleep_seconds: float
    max_rounds: int

    # --- Logging -----------------------------------------------------------
    log_file: Path | None
    log_level: str
    log_max_bytes: int
    log_backup_count: int

    # --- Notifications -----------------------------------------------------
    telegram_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str

    # --- Raw values, kept for diagnostics ----------------------------------
    raw: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def total_memory_per_ocpu(self) -> float:
        """GB of RAM per OCPU, or 0.0 when ocpus is not usable."""
        return self.memory_gb / self.ocpus if self.ocpus > 0 else 0.0


class ConfigError(Exception):
    """Raised when configuration cannot be used to launch an instance."""


# --------------------------------------------------------------------------- #
# Primitive coercion helpers. Each records a message instead of raising.
# --------------------------------------------------------------------------- #
def _as_float(key: str, value: str, errors: list[str]) -> float:
    try:
        return float(value)
    except ValueError:
        errors.append(f"{key}: {value!r} is not a number (using default {DEFAULTS.get(key)})")
        return float(DEFAULTS.get(key, "0"))


def _as_int(key: str, value: str, errors: list[str]) -> int:
    try:
        return int(float(value))
    except ValueError:
        errors.append(f"{key}: {value!r} is not an integer (using default {DEFAULTS.get(key)})")
        return int(float(DEFAULTS.get(key, "0")))


def _as_bool(key: str, value: str, errors: list[str]) -> bool:
    lowered = value.strip().lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    errors.append(f"{key}: {value!r} is not a boolean (expected true/false)")
    return False


def _as_list(value: str) -> list[str]:
    """Split a comma-separated (or newline-separated) string into clean items."""
    return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]


# --------------------------------------------------------------------------- #
# Source loaders
# --------------------------------------------------------------------------- #
def _load_yaml(path: Path, errors: list[str]) -> dict[str, str]:
    """Read a flat config.yaml into UPPER_SNAKE string keys."""
    if not path.is_file():
        return {}
    try:
        import yaml  # imported lazily so the tool still runs without PyYAML
    except ImportError:  # pragma: no cover - only hit on a broken install
        errors.append(f"{path} exists but PyYAML is not installed; ignoring it")
        return {}

    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as a config error
        errors.append(f"{path}: cannot parse YAML ({exc})")
        return {}

    if not isinstance(data, dict):
        errors.append(f"{path}: expected a mapping at the top level")
        return {}

    flat: dict[str, str] = {}
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = ",".join(str(item) for item in value)
        if isinstance(value, bool):
            value = "true" if value else "false"
        flat[str(key).upper()] = str(value)
    return flat


def _load_dotenv(path: Path) -> dict[str, str]:
    """Read a .env file without letting it clobber real environment variables."""
    if not path.is_file():
        return {}
    from dotenv import dotenv_values

    return {k.upper(): v for k, v in dotenv_values(path).items() if v is not None}


def load_raw(env_file: Path, yaml_file: Path, errors: list[str]) -> dict[str, str]:
    """Merge every configuration source into a single flat string mapping."""
    merged: dict[str, str] = dict(DEFAULTS)
    merged.update(_load_yaml(yaml_file, errors))
    merged.update(_load_dotenv(env_file))

    # Real environment wins, but only for keys we actually know about.
    for key in set(merged) | set(REQUIRED_KEYS):
        env_value = os.environ.get(key)
        if env_value is not None:
            merged[key] = env_value

    for key in REQUIRED_KEYS:
        merged.setdefault(key, "")
    return merged


def expand_path(value: str) -> Path:
    """Expand `~` and environment variables in a path-ish config value."""
    return Path(os.path.expandvars(value)).expanduser()


def build_config(raw: dict[str, str]) -> tuple[AppConfig, list[str]]:
    """Turn the raw string mapping into a typed AppConfig plus coercion errors."""
    errors: list[str] = []

    log_file_raw = raw.get("LOG_FILE", "").strip()
    fault_domain_raw = raw.get("FAULT_DOMAIN", "").strip()

    cfg = AppConfig(
        compartment_id=raw.get("COMPARTMENT_ID", "").strip(),
        subnet_id=raw.get("SUBNET_ID", "").strip(),
        image_id=raw.get("IMAGE_ID", "").strip(),
        oci_config_file=expand_path(raw.get("OCI_CONFIG_FILE", DEFAULTS["OCI_CONFIG_FILE"])),
        oci_profile=raw.get("OCI_PROFILE", "DEFAULT").strip() or "DEFAULT",
        shape=raw.get("SHAPE", A1_SHAPE).strip() or A1_SHAPE,
        display_name=raw.get("DISPLAY_NAME", "").strip(),
        ocpus=_as_float("OCPUS", raw.get("OCPUS", DEFAULTS["OCPUS"]), errors),
        memory_gb=_as_float("MEMORY_GB", raw.get("MEMORY_GB", DEFAULTS["MEMORY_GB"]), errors),
        boot_volume_gb=_as_int(
            "BOOT_VOLUME_GB", raw.get("BOOT_VOLUME_GB", DEFAULTS["BOOT_VOLUME_GB"]), errors
        ),
        ssh_public_key_path=expand_path(raw.get("SSH_PUBLIC_KEY_PATH", "")),
        assign_public_ip=_as_bool(
            "ASSIGN_PUBLIC_IP", raw.get("ASSIGN_PUBLIC_IP", DEFAULTS["ASSIGN_PUBLIC_IP"]), errors
        ),
        availability_domains=_as_list(raw.get("AVAILABILITY_DOMAINS", "")),
        fault_domain=fault_domain_raw or None,
        nsg_ids=_as_list(raw.get("NSG_IDS", "")),
        min_delay_seconds=_as_float(
            "MIN_DELAY_SECONDS", raw.get("MIN_DELAY_SECONDS", DEFAULTS["MIN_DELAY_SECONDS"]), errors
        ),
        max_delay_seconds=_as_float(
            "MAX_DELAY_SECONDS", raw.get("MAX_DELAY_SECONDS", DEFAULTS["MAX_DELAY_SECONDS"]), errors
        ),
        ad_delay_seconds=_as_float(
            "AD_DELAY_SECONDS", raw.get("AD_DELAY_SECONDS", DEFAULTS["AD_DELAY_SECONDS"]), errors
        ),
        rate_limit_sleep_seconds=_as_float(
            "RATE_LIMIT_SLEEP_SECONDS",
            raw.get("RATE_LIMIT_SLEEP_SECONDS", DEFAULTS["RATE_LIMIT_SLEEP_SECONDS"]),
            errors,
        ),
        max_rounds=_as_int("MAX_ROUNDS", raw.get("MAX_ROUNDS", DEFAULTS["MAX_ROUNDS"]), errors),
        log_file=expand_path(log_file_raw) if log_file_raw else None,
        log_level=raw.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        log_max_bytes=_as_int("LOG_MAX_BYTES", raw.get("LOG_MAX_BYTES", DEFAULTS["LOG_MAX_BYTES"]), errors),
        log_backup_count=_as_int(
            "LOG_BACKUP_COUNT", raw.get("LOG_BACKUP_COUNT", DEFAULTS["LOG_BACKUP_COUNT"]), errors
        ),
        telegram_enabled=_as_bool(
            "TELEGRAM_ENABLED", raw.get("TELEGRAM_ENABLED", DEFAULTS["TELEGRAM_ENABLED"]), errors
        ),
        telegram_bot_token=raw.get("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=raw.get("TELEGRAM_CHAT_ID", "").strip(),
        raw=raw,
    )

    if cfg.min_delay_seconds > cfg.max_delay_seconds:
        errors.append(
            f"MIN_DELAY_SECONDS ({cfg.min_delay_seconds:g}) is greater than "
            f"MAX_DELAY_SECONDS ({cfg.max_delay_seconds:g})"
        )
    return cfg, errors


def load_config(env_file: Path, yaml_file: Path) -> tuple[AppConfig, list[str]]:
    """Load configuration from all sources. Returns (config, non-fatal errors)."""
    errors: list[str] = []
    raw = load_raw(env_file, yaml_file, errors)
    cfg, build_errors = build_config(raw)
    return cfg, errors + build_errors
