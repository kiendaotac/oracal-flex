"""The retry loop that eventually wins a VM.Standard.A1.Flex instance."""

from __future__ import annotations

import json
import logging
import random
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone

import oci
from oci.core.models import (
    CreateVnicDetails,
    InstanceSourceViaImageDetails,
    LaunchInstanceDetails,
    LaunchInstanceShapeConfigDetails,
)
from oci.exceptions import ServiceError

from .config import AppConfig
from .errors import ErrorKind, classify, describe
from .notify import send_telegram
from .oci_clients import OciClients, read_ssh_public_key, resolve_availability_domains
from .shutdown import Shutdown

logger = logging.getLogger("a1launcher.launcher")

INSTANCE_RUNNING_TIMEOUT_SECONDS = 900


class QuotaExhausted(Exception):
    """The tenancy has no Always Free A1 capacity budget left. Retrying is pointless."""


@dataclass(slots=True)
class LaunchResult:
    instance_id: str
    availability_domain: str
    display_name: str
    public_ip: str | None = None
    private_ip: str | None = None

    def summary(self) -> str:
        return (
            f"Instance OCID : {self.instance_id}\n"
            f"Display name  : {self.display_name}\n"
            f"Availability  : {self.availability_domain}\n"
            f"Public IP     : {self.public_ip or '(none yet)'}\n"
            f"Private IP    : {self.private_ip or '(unknown)'}"
        )


def build_launch_details(cfg: AppConfig, availability_domain: str, ssh_key: str) -> LaunchInstanceDetails:
    """Assemble the LaunchInstanceDetails payload for one availability domain."""
    vnic_details = CreateVnicDetails(
        subnet_id=cfg.subnet_id,
        assign_public_ip=cfg.assign_public_ip,
    )
    if cfg.nsg_ids:
        vnic_details.nsg_ids = cfg.nsg_ids

    details = LaunchInstanceDetails(
        availability_domain=availability_domain,
        compartment_id=cfg.compartment_id,
        display_name=cfg.display_name,
        shape=cfg.shape,
        shape_config=LaunchInstanceShapeConfigDetails(
            ocpus=cfg.ocpus,
            memory_in_gbs=cfg.memory_gb,
        ),
        source_details=InstanceSourceViaImageDetails(
            image_id=cfg.image_id,
            boot_volume_size_in_gbs=cfg.boot_volume_gb,
        ),
        create_vnic_details=vnic_details,
        metadata={"ssh_authorized_keys": ssh_key},
    )
    if cfg.fault_domain:
        details.fault_domain = cfg.fault_domain
    return details


def payload_as_json(details: LaunchInstanceDetails) -> str:
    """Render a launch payload as pretty JSON for --dry-run output."""
    try:
        data = oci.util.to_dict(details)
    except Exception:  # noqa: BLE001 - fall back to the SDK's own repr
        return repr(details)
    return json.dumps(data, indent=2, sort_keys=True, default=str)


class Launcher:
    """Owns the retry loop, AD rotation, and backoff policy."""

    def __init__(
        self,
        cfg: AppConfig,
        clients: OciClients,
        shutdown: Shutdown,
        dry_run: bool = False,
    ) -> None:
        self.cfg = cfg
        self.clients = clients
        self.shutdown = shutdown
        self.dry_run = dry_run
        self.attempts = 0
        self.capacity_errors = 0
        self.started_at = datetime.now(timezone.utc)

    # ----------------------------------------------------------------- #
    # Single attempt
    # ----------------------------------------------------------------- #
    def attempt(self, availability_domain: str, ssh_key: str) -> LaunchResult | None:
        """Try one launch. Returns a LaunchResult on success, None to keep retrying.

        Raises QuotaExhausted when the failure means no amount of retrying helps.
        """
        details = build_launch_details(self.cfg, availability_domain, ssh_key)
        self.attempts += 1

        if self.dry_run:
            logger.info(
                "[dry-run] attempt #%d in %s — payload below, nothing was sent",
                self.attempts,
                availability_domain,
            )
            print(payload_as_json(details))
            return None

        logger.info("Attempt #%d: launching %s in %s", self.attempts, self.cfg.shape, availability_domain)
        try:
            response = self.clients.compute.launch_instance(details)
        except ServiceError as exc:
            self._handle_service_error(exc, availability_domain)
            return None
        except Exception:  # noqa: BLE001 - network hiccups etc. must not kill the loop
            logger.error(
                "Unexpected error launching in %s:\n%s", availability_domain, traceback.format_exc()
            )
            return None

        instance = response.data
        logger.info("SUCCESS — instance %s created in %s", instance.id, availability_domain)
        return LaunchResult(
            instance_id=instance.id,
            availability_domain=availability_domain,
            display_name=instance.display_name or self.cfg.display_name,
        )

    def _handle_service_error(self, exc: ServiceError, availability_domain: str) -> None:
        """Log a ServiceError according to its classification, or abort on quota."""
        kind = classify(exc)

        if kind is ErrorKind.OUT_OF_CAPACITY:
            self.capacity_errors += 1
            logger.info(
                "Out of host capacity in %s (%d so far) — trying the next AD",
                availability_domain,
                self.capacity_errors,
            )
        elif kind is ErrorKind.QUOTA_EXCEEDED:
            logger.error("Service limit / quota exceeded: %s", describe(exc))
            raise QuotaExhausted(exc.message or "limit exceeded") from exc
        elif kind is ErrorKind.RATE_LIMITED:
            logger.warning("Rate limited by OCI: %s", describe(exc))
            self._sleep_rate_limited()
        elif kind is ErrorKind.AUTH:
            # Per spec we keep retrying, but this almost always means bad config.
            logger.warning(
                "Authorization/not-found error in %s: %s. Retrying will not fix a wrong OCID "
                "or missing IAM policy — re-run with --check.",
                availability_domain,
                describe(exc),
            )
        else:
            logger.error(
                "Unhandled service error in %s: %s\n%s",
                availability_domain,
                describe(exc),
                traceback.format_exc(),
            )

    def _sleep_rate_limited(self) -> None:
        seconds = self.cfg.rate_limit_sleep_seconds
        logger.warning("Backing off for %.0fs after a 429 response", seconds)
        self.shutdown.sleep(seconds)

    # ----------------------------------------------------------------- #
    # Post-launch details
    # ----------------------------------------------------------------- #
    def enrich_with_ip(self, result: LaunchResult) -> LaunchResult:
        """Wait for RUNNING and fill in the public/private IP addresses.

        Failure here is logged but not fatal: the instance exists either way, and
        reporting a missing IP beats exiting non-zero on a successful launch.
        """
        try:
            logger.info("Waiting for instance to reach RUNNING (up to %ds)...", INSTANCE_RUNNING_TIMEOUT_SECONDS)
            oci.wait_until(
                self.clients.compute,
                self.clients.compute.get_instance(result.instance_id),
                "lifecycle_state",
                "RUNNING",
                max_wait_seconds=INSTANCE_RUNNING_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not confirm RUNNING state: %s", exc)

        try:
            attachments = self.clients.compute.list_vnic_attachments(
                compartment_id=self.cfg.compartment_id, instance_id=result.instance_id
            ).data
            for attachment in attachments:
                if not attachment.vnic_id:
                    continue
                vnic = self.clients.network.get_vnic(attachment.vnic_id).data
                result.public_ip = result.public_ip or vnic.public_ip
                result.private_ip = result.private_ip or vnic.private_ip
                if result.public_ip:
                    break
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read the instance IP addresses: %s", exc)
        return result

    def notify(self, result: LaunchResult) -> None:
        if not self.cfg.telegram_enabled:
            return
        elapsed = datetime.now(timezone.utc) - self.started_at
        text = (
            "✅ Oracle Cloud A1.Flex instance created\n\n"
            f"{result.summary()}\n\n"
            f"Shape: {self.cfg.shape} ({self.cfg.ocpus:g} OCPU / {self.cfg.memory_gb:g} GB)\n"
            f"Attempts: {self.attempts}\n"
            f"Elapsed: {str(elapsed).split('.')[0]}"
        )
        send_telegram(self.cfg.telegram_bot_token, self.cfg.telegram_chat_id, text)

    # ----------------------------------------------------------------- #
    # Main loop
    # ----------------------------------------------------------------- #
    def _round_delay(self) -> float:
        """Randomised backoff between full passes over all availability domains."""
        return random.uniform(self.cfg.min_delay_seconds, self.cfg.max_delay_seconds)

    def run(self) -> int:
        """Retry until an instance is created. Returns the process exit code."""
        ssh_key = read_ssh_public_key(self.cfg.ssh_public_key_path)
        domains = resolve_availability_domains(self.clients, self.cfg)
        if not domains:
            logger.error("No availability domains to try")
            return 1

        logger.info(
            "Starting retry loop: %s %.4g OCPU / %.4g GB, %d GB boot, %d availability domain(s): %s",
            self.cfg.shape,
            self.cfg.ocpus,
            self.cfg.memory_gb,
            self.cfg.boot_volume_gb,
            len(domains),
            ", ".join(domains),
        )
        if self.dry_run:
            logger.warning("DRY RUN — no instance will be created")

        round_number = 0
        while not self.shutdown.requested:
            round_number += 1
            if self.cfg.max_rounds and round_number > self.cfg.max_rounds:
                logger.error("Reached MAX_ROUNDS=%d without success", self.cfg.max_rounds)
                return 1

            logger.info("--- Round %d (%d attempts so far) ---", round_number, self.attempts)

            for index, availability_domain in enumerate(domains):
                if self.shutdown.requested:
                    break
                try:
                    result = self.attempt(availability_domain, ssh_key)
                except QuotaExhausted as exc:
                    logger.error(
                        "Always Free A1 quota is exhausted (%s). Delete an existing A1 instance "
                        "or lower OCPUS/MEMORY_GB, then run again.",
                        exc,
                    )
                    return 1

                if result is not None:
                    return self._finish(result)

                # Space out calls inside a round so we do not trip the rate limiter.
                is_last = index == len(domains) - 1
                if not is_last and not self.shutdown.sleep(self.cfg.ad_delay_seconds):
                    break

            if self.dry_run:
                logger.info("DRY RUN complete after one round")
                return 0

            if self.shutdown.requested:
                break

            delay = self._round_delay()
            logger.info("Round %d found no capacity; sleeping %.0fs", round_number, delay)
            self.shutdown.sleep(delay)

        logger.info(
            "Shutting down after %s: %d attempt(s), %d out-of-capacity response(s), no instance created",
            self.shutdown.signal_name or "stop request",
            self.attempts,
            self.capacity_errors,
        )
        return 130

    def _finish(self, result: LaunchResult) -> int:
        """Enrich, report, and notify about a successful launch."""
        self.enrich_with_ip(result)
        elapsed = datetime.now(timezone.utc) - self.started_at

        print("\n" + "=" * 72)
        print("INSTANCE CREATED")
        print("=" * 72)
        print(result.summary())
        print(f"Attempts      : {self.attempts}")
        print(f"Elapsed       : {str(elapsed).split('.')[0]}")
        print("=" * 72 + "\n")

        logger.info("Instance %s ready, public IP %s", result.instance_id, result.public_ip)
        self.notify(result)
        return 0
