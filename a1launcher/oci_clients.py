"""Thin helpers around OCI SDK client construction and read-only lookups."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import oci
from oci.core import ComputeClient, VirtualNetworkClient
from oci.identity import IdentityClient

from .config import AppConfig

logger = logging.getLogger("a1launcher.oci")


@dataclass(slots=True)
class OciClients:
    """The three OCI clients this tool needs, plus the config they were built from."""

    config: dict
    identity: IdentityClient
    compute: ComputeClient
    network: VirtualNetworkClient


def load_oci_config(config_file: Path, profile: str) -> dict:
    """Load ~/.oci/config for the given profile and validate it."""
    oci_config = oci.config.from_file(file_location=str(config_file), profile_name=profile)
    oci.config.validate_config(oci_config)
    return oci_config


def build_clients(cfg: AppConfig) -> OciClients:
    """Construct identity/compute/network clients from the OCI config file."""
    oci_config = load_oci_config(cfg.oci_config_file, cfg.oci_profile)
    return OciClients(
        config=oci_config,
        identity=IdentityClient(oci_config),
        compute=ComputeClient(oci_config),
        network=VirtualNetworkClient(oci_config),
    )


def list_availability_domains(identity: IdentityClient, compartment_id: str) -> list[str]:
    """Return every availability domain name visible in the compartment."""
    response = identity.list_availability_domains(compartment_id=compartment_id)
    return [ad.name for ad in response.data]


def resolve_availability_domains(clients: OciClients, cfg: AppConfig) -> list[str]:
    """Discover the ADs to try, honouring an explicit AVAILABILITY_DOMAINS filter.

    A configured name that does not exist in the tenancy is dropped with a
    warning rather than failing the run, so one typo cannot block the others.
    """
    discovered = list_availability_domains(clients.identity, cfg.compartment_id)
    if not cfg.availability_domains:
        return discovered

    selected: list[str] = []
    for wanted in cfg.availability_domains:
        matches = [ad for ad in discovered if ad == wanted or ad.endswith(wanted)]
        if matches:
            selected.extend(m for m in matches if m not in selected)
        else:
            logger.warning("Configured availability domain %r not found in tenancy", wanted)
    return selected or discovered


def read_ssh_public_key(path: Path) -> str:
    """Read the SSH public key, stripped of trailing whitespace."""
    return path.read_text(encoding="utf-8").strip()
