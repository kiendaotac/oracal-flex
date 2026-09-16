"""Preflight checks: validate everything before a single instance is launched."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import oci
from oci.exceptions import ConfigFileNotFound, InvalidConfig, InvalidKeyFilePath, ProfileNotFound, ServiceError

from .config import (
    FREE_TIER_MAX_GB_PER_OCPU,
    FREE_TIER_MAX_MEMORY_GB,
    FREE_TIER_MAX_OCPUS,
    REQUIRED_KEYS,
    AppConfig,
)
from .errors import describe
from .oci_clients import build_clients, list_availability_domains

logger = logging.getLogger("a1launcher.preflight")

OCID_RE = re.compile(r"^ocid1\.[a-z0-9]+\.[a-z0-9-]+\.[a-z0-9-]*\.[a-zA-Z0-9._-]+$")
SSH_KEY_PREFIXES = ("ssh-ed25519", "ssh-rsa", "ecdsa-")
PRIVATE_KEY_MARKER = "PRIVATE KEY"


class Status(Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(slots=True)
class CheckResult:
    name: str
    status: Status
    detail: str = ""
    extra: list[str] | None = None

    @property
    def failed(self) -> bool:
        return self.status is Status.FAIL


def _ok(name: str, detail: str = "", extra: list[str] | None = None) -> CheckResult:
    return CheckResult(name, Status.PASS, detail, extra)


def _warn(name: str, detail: str, extra: list[str] | None = None) -> CheckResult:
    return CheckResult(name, Status.WARN, detail, extra)


def _fail(name: str, detail: str, extra: list[str] | None = None) -> CheckResult:
    return CheckResult(name, Status.FAIL, detail, extra)


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #
def check_sources(env_file: Path, yaml_file: Path) -> CheckResult:
    """Report which configuration sources were actually found."""
    found = [str(p) for p in (yaml_file, env_file) if p.is_file()]
    if found:
        return _ok("Config sources", ", ".join(found))
    return _warn(
        "Config sources",
        f"neither {yaml_file} nor {env_file} found; using environment variables and defaults only",
    )


def check_required_values(cfg: AppConfig) -> list[CheckResult]:
    """Every required variable must be present and non-empty."""
    results: list[CheckResult] = []
    values = {
        "COMPARTMENT_ID": cfg.compartment_id,
        "SUBNET_ID": cfg.subnet_id,
        "IMAGE_ID": cfg.image_id,
        "SSH_PUBLIC_KEY_PATH": str(cfg.ssh_public_key_path),
        "DISPLAY_NAME": cfg.display_name,
    }
    for key in REQUIRED_KEYS:
        value = values[key].strip()
        if value:
            results.append(_ok(f"Required value {key}", value))
        else:
            results.append(_fail(f"Required value {key}", "missing or empty"))
    return results


def check_config_errors(config_errors: list[str]) -> list[CheckResult]:
    """Surface coercion problems collected while loading the configuration."""
    if not config_errors:
        return [_ok("Config value parsing", "all values parsed cleanly")]
    return [_fail("Config value parsing", err) for err in config_errors]


def _check_ocid(label: str, value: str, prefixes: tuple[str, ...]) -> CheckResult:
    name = f"OCID format {label}"
    if not value:
        return _fail(name, "empty")
    if not value.startswith(prefixes):
        expected = " or ".join(prefixes)
        return _fail(name, f"expected it to start with {expected}, got {value[:40]!r}")
    if not OCID_RE.match(value):
        return _fail(name, f"does not look like a well-formed OCID: {value[:60]!r}")
    return _ok(name, value)


def check_ocids(cfg: AppConfig) -> list[CheckResult]:
    return [
        _check_ocid("compartment_id", cfg.compartment_id, ("ocid1.tenancy", "ocid1.compartment")),
        _check_ocid("subnet_id", cfg.subnet_id, ("ocid1.subnet",)),
        _check_ocid("image_id", cfg.image_id, ("ocid1.image",)),
    ]


def check_shape_resources(cfg: AppConfig) -> list[CheckResult]:
    """Keep the requested shape inside the Always Free A1 envelope."""
    results: list[CheckResult] = []

    if cfg.ocpus <= 0:
        results.append(_fail("OCPU count", f"must be greater than 0, got {cfg.ocpus:g}"))
    elif cfg.ocpus > FREE_TIER_MAX_OCPUS:
        results.append(
            _fail(
                "OCPU count",
                f"{cfg.ocpus:g} exceeds the Always Free maximum of {FREE_TIER_MAX_OCPUS:g} OCPUs",
            )
        )
    else:
        results.append(
            _ok("OCPU count", f"{cfg.ocpus:g} of {FREE_TIER_MAX_OCPUS:g} free-tier OCPUs")
        )

    if cfg.memory_gb <= 0:
        results.append(_fail("Memory", f"must be greater than 0, got {cfg.memory_gb:g} GB"))
    elif cfg.memory_gb > FREE_TIER_MAX_MEMORY_GB:
        results.append(
            _fail(
                "Memory",
                f"{cfg.memory_gb:g} GB exceeds the Always Free maximum of "
                f"{FREE_TIER_MAX_MEMORY_GB:g} GB",
            )
        )
    else:
        results.append(
            _ok("Memory", f"{cfg.memory_gb:g} GB of {FREE_TIER_MAX_MEMORY_GB:g} free-tier GB")
        )

    if cfg.ocpus > 0:
        ratio = cfg.total_memory_per_ocpu
        if ratio > FREE_TIER_MAX_GB_PER_OCPU:
            results.append(
                _fail(
                    "Memory/OCPU ratio",
                    f"{ratio:.2f} GB per OCPU exceeds the A1.Flex maximum of "
                    f"{FREE_TIER_MAX_GB_PER_OCPU:g} GB per OCPU",
                )
            )
        else:
            results.append(
                _ok(
                    "Memory/OCPU ratio",
                    f"{ratio:.2f} GB per OCPU (limit {FREE_TIER_MAX_GB_PER_OCPU:g})",
                )
            )

    if cfg.boot_volume_gb < 50:
        results.append(
            _fail("Boot volume", f"{cfg.boot_volume_gb} GB is below the 50 GB minimum")
        )
    elif cfg.boot_volume_gb > 200:
        results.append(
            _warn(
                "Boot volume",
                f"{cfg.boot_volume_gb} GB; Always Free block storage totals 200 GB across all "
                "volumes, so this may incur charges",
            )
        )
    else:
        results.append(_ok("Boot volume", f"{cfg.boot_volume_gb} GB"))

    remaining_ocpus = FREE_TIER_MAX_OCPUS - cfg.ocpus
    remaining_memory = FREE_TIER_MAX_MEMORY_GB - cfg.memory_gb
    if remaining_ocpus >= 0 and remaining_memory >= 0:
        results.append(
            _ok(
                "Free-tier headroom",
                f"{remaining_ocpus:g} OCPU / {remaining_memory:g} GB left for other A1 instances",
            )
        )
    return results


def check_oci_config_file(cfg: AppConfig) -> list[CheckResult]:
    """The OCI config file must exist, parse, and point at a readable key file."""
    results: list[CheckResult] = []
    path = cfg.oci_config_file

    if not path.is_file():
        return [_fail("OCI config file", f"{path} not found")]
    if not os.access(path, os.R_OK):
        return [_fail("OCI config file", f"{path} is not readable")]
    results.append(_ok("OCI config file", str(path)))

    try:
        oci_config = oci.config.from_file(file_location=str(path), profile_name=cfg.oci_profile)
        oci.config.validate_config(oci_config)
    except ProfileNotFound as exc:
        results.append(_fail("OCI profile", f"profile {cfg.oci_profile!r} not found: {exc}"))
        return results
    except (ConfigFileNotFound, InvalidConfig, InvalidKeyFilePath) as exc:
        results.append(_fail("OCI profile", f"profile {cfg.oci_profile!r} is invalid: {exc}"))
        return results

    results.append(
        _ok(
            "OCI profile",
            f"{cfg.oci_profile} (region={oci_config.get('region')}, "
            f"tenancy={str(oci_config.get('tenancy'))[:30]}...)",
        )
    )

    key_file = oci_config.get("key_file")
    if not key_file:
        results.append(_warn("OCI key_file", "no key_file in profile (token-based auth?)"))
        return results

    key_path = Path(key_file).expanduser()
    if not key_path.is_file():
        results.append(
            _fail(
                "OCI key_file",
                f"{key_path} does not exist. Inside Docker this path must be the path "
                "as seen INSIDE the container, not the host path.",
            )
        )
    elif not os.access(key_path, os.R_OK):
        results.append(_fail("OCI key_file", f"{key_path} is not readable"))
    else:
        results.append(_ok("OCI key_file", str(key_path)))
    return results


def check_ssh_public_key(cfg: AppConfig) -> list[CheckResult]:
    """Validate the SSH public key and print it for a human eyeball check."""
    results: list[CheckResult] = []
    path = cfg.ssh_public_key_path

    if not path.is_file():
        return [_fail("SSH public key file", f"{path} not found")]

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [_fail("SSH public key file", f"cannot read {path}: {exc}")]

    results.append(_ok("SSH public key file", str(path)))

    if PRIVATE_KEY_MARKER in content:
        results.append(
            _fail(
                "SSH key type",
                f"{path} contains {PRIVATE_KEY_MARKER!r} — this is a PRIVATE key. "
                "Point SSH_PUBLIC_KEY_PATH at the .pub file instead.",
            )
        )
        return results

    stripped = content.strip()
    if not stripped:
        return results + [_fail("SSH key content", f"{path} is empty")]

    lines = [line for line in stripped.splitlines() if line.strip()]
    if len(lines) != 1:
        results.append(
            _fail(
                "SSH key content",
                f"expected exactly 1 key line, found {len(lines)}. "
                "Multiple keys are not supported by this tool.",
            )
        )
    elif not stripped.startswith(SSH_KEY_PREFIXES):
        results.append(
            _fail(
                "SSH key content",
                f"must start with one of {', '.join(SSH_KEY_PREFIXES)}; got {stripped[:30]!r}",
            )
        )
    else:
        key_type = stripped.split(maxsplit=1)[0]
        results.append(_ok("SSH key content", f"valid {key_type} public key, single line"))

    # Printed so the operator can confirm it is the key they expect.
    results.append(_ok("SSH key (for visual check)", "", extra=[stripped]))
    return results


def check_api_access(cfg: AppConfig) -> list[CheckResult]:
    """Make one read-only API call to prove credentials and permissions work."""
    try:
        clients = build_clients(cfg)
    except Exception as exc:  # noqa: BLE001 - any client build failure is a FAIL
        return [_fail("OCI client", f"cannot build OCI clients: {exc}")]

    try:
        domains = list_availability_domains(clients.identity, cfg.compartment_id)
    except ServiceError as exc:
        return [_fail("API read-only call (ListAvailabilityDomains)", describe(exc))]
    except Exception as exc:  # noqa: BLE001 - network/SSL/etc.
        return [_fail("API read-only call (ListAvailabilityDomains)", f"{type(exc).__name__}: {exc}")]

    if not domains:
        return [_fail("API read-only call (ListAvailabilityDomains)", "no availability domains returned")]

    results = [
        _ok(
            "API read-only call (ListAvailabilityDomains)",
            f"{len(domains)} availability domain(s) found",
            extra=domains,
        )
    ]

    if cfg.availability_domains:
        unknown = [
            wanted
            for wanted in cfg.availability_domains
            if not any(ad == wanted or ad.endswith(wanted) for ad in domains)
        ]
        if unknown:
            results.append(
                _warn(
                    "AVAILABILITY_DOMAINS filter",
                    f"these configured names match no AD and will be skipped: {', '.join(unknown)}",
                )
            )
        else:
            results.append(
                _ok("AVAILABILITY_DOMAINS filter", ", ".join(cfg.availability_domains))
            )
    return results


def check_telegram(cfg: AppConfig) -> list[CheckResult]:
    if not cfg.telegram_enabled:
        return [_ok("Telegram notification", "disabled")]
    missing = [
        name
        for name, value in (("TELEGRAM_BOT_TOKEN", cfg.telegram_bot_token), ("TELEGRAM_CHAT_ID", cfg.telegram_chat_id))
        if not value
    ]
    if missing:
        return [_fail("Telegram notification", f"enabled but {', '.join(missing)} is empty")]
    return [_ok("Telegram notification", f"enabled for chat {cfg.telegram_chat_id}")]


# --------------------------------------------------------------------------- #
# Runner / reporting
# --------------------------------------------------------------------------- #
def run_preflight(
    cfg: AppConfig,
    config_errors: list[str],
    env_file: Path,
    yaml_file: Path,
    skip_api: bool = False,
) -> list[CheckResult]:
    """Run every preflight check and return the results in report order."""
    results: list[CheckResult] = [check_sources(env_file, yaml_file)]
    results.extend(check_config_errors(config_errors))
    results.extend(check_required_values(cfg))
    results.extend(check_ocids(cfg))
    results.extend(check_shape_resources(cfg))
    results.extend(check_oci_config_file(cfg))
    results.extend(check_ssh_public_key(cfg))
    results.extend(check_telegram(cfg))

    if skip_api:
        results.append(_warn("API read-only call (ListAvailabilityDomains)", "skipped (--no-api-check)"))
    else:
        results.extend(check_api_access(cfg))
    return results


def report(results: list[CheckResult]) -> bool:
    """Print the PASS/FAIL report. Returns True when no check failed."""
    width = max(len(r.name) for r in results) + 2
    print("\n" + "=" * 72)
    print("PREFLIGHT CHECK")
    print("=" * 72)

    for result in results:
        line = f"[{result.status.value:<4}] {result.name:<{width}}"
        if result.detail:
            line += f" {result.detail}"
        print(line)
        for extra in result.extra or []:
            for extra_line in str(extra).splitlines():
                print(f"         | {extra_line}")

    failed = [r for r in results if r.failed]
    warned = [r for r in results if r.status is Status.WARN]
    print("-" * 72)
    print(
        f"{len(results) - len(failed) - len(warned)} passed, "
        f"{len(warned)} warning(s), {len(failed)} failed"
    )
    if failed:
        print("\nFAILED CHECKS:")
        for result in failed:
            print(f"  - {result.name}: {result.detail}")
        print("\nFix the items above before running a real launch.")
    else:
        print("\nAll checks passed. Safe to run the launcher.")
    print("=" * 72 + "\n")
    return not failed
