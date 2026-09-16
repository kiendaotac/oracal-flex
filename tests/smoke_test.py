#!/usr/bin/env python3
"""Offline smoke tests: no OCI account, no network, no pytest.

Drives the retry loop against fake OCI clients to prove the behaviour that
matters: capacity errors rotate availability domains, quota errors abort,
--dry-run never calls LaunchInstance, and errors are classified correctly.

Run with:  python tests/smoke_test.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from oci.exceptions import ServiceError  # noqa: E402

from a1launcher.config import load_config  # noqa: E402
from a1launcher.errors import ErrorKind, classify  # noqa: E402
from a1launcher.launcher import Launcher, build_launch_details  # noqa: E402
from a1launcher.logging_setup import setup_logging  # noqa: E402
from a1launcher.oci_clients import OciClients  # noqa: E402
from a1launcher.shutdown import Shutdown  # noqa: E402

ADS = ["fake:AD-1", "fake:AD-2", "fake:AD-3"]
CAPACITY_ERROR = ServiceError(500, "InternalError", {}, "Out of host capacity.")

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


# --------------------------------------------------------------------------- #
# Fake OCI clients
# --------------------------------------------------------------------------- #
class FakeIdentity:
    def list_availability_domains(self, compartment_id: str):
        return SimpleNamespace(data=[SimpleNamespace(name=name) for name in ADS])


class FakeCompute:
    """Replays a scripted sequence of outcomes, one per launch_instance call."""

    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.calls = 0

    def launch_instance(self, details):
        self.calls += 1
        outcome = self.script.pop(0) if self.script else "ok"
        if isinstance(outcome, ServiceError):
            raise outcome
        return SimpleNamespace(
            data=SimpleNamespace(id="ocid1.instance.oc1..fake", display_name=details.display_name)
        )

    def get_instance(self, instance_id: str):
        raise RuntimeError("no real waiter in tests")

    def list_vnic_attachments(self, **kwargs):
        return SimpleNamespace(data=[SimpleNamespace(vnic_id="ocid1.vnic.oc1..fake")])


class ExplodingCompute:
    def launch_instance(self, details):
        raise AssertionError("--dry-run must never call LaunchInstance")


class FakeNetwork:
    def get_vnic(self, vnic_id: str):
        return SimpleNamespace(
            data=SimpleNamespace(public_ip="140.238.1.2", private_ip="10.0.0.5")
        )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def write_fixture_env(directory: Path) -> Path:
    """A minimal but complete .env plus the SSH public key it points at."""
    key_path = directory / "test_key.pub"
    key_path.write_text(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyForTestsOnly000000000000 test@ci\n",
        encoding="utf-8",
    )
    env_path = directory / ".env"
    env_path.write_text(
        "\n".join(
            [
                "COMPARTMENT_ID=ocid1.tenancy.oc1..aaaaaaaabcdefghijklmnopqrstuvwxyz1234567890",
                "SUBNET_ID=ocid1.subnet.oc1.ap-singapore-1.aaaaaaaabcdefghijklmnop1234567890",
                "IMAGE_ID=ocid1.image.oc1.ap-singapore-1.aaaaaaaabcdefghijklmnop1234567890",
                f"SSH_PUBLIC_KEY_PATH={key_path}",
                "DISPLAY_NAME=a1-test",
                "OCPUS=2",
                "MEMORY_GB=12",
                f"LOG_FILE={directory / 'a1.log'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return env_path


def load_fast_config(env_path: Path):
    """Load the fixture config with all the waiting turned off."""
    cfg, errors = load_config(env_path, Path("/nonexistent.yaml"))
    assert not errors, errors
    cfg.min_delay_seconds = cfg.max_delay_seconds = 0.0
    cfg.ad_delay_seconds = 0.0
    return cfg


def run_launcher(cfg, script: list[object], dry_run: bool = False) -> tuple[int, int]:
    compute = ExplodingCompute() if dry_run else FakeCompute(script)
    clients = OciClients(
        config={}, identity=FakeIdentity(), compute=compute, network=FakeNetwork()
    )
    code = Launcher(cfg, clients, Shutdown(), dry_run=dry_run).run()
    return code, getattr(compute, "calls", 0)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_error_classification() -> None:
    print("\n[1] error classification")
    cases: list[tuple[ServiceError, ErrorKind]] = [
        (CAPACITY_ERROR, ErrorKind.OUT_OF_CAPACITY),
        (ServiceError(500, "OutOfCapacity", {}, "out of capacity"), ErrorKind.OUT_OF_CAPACITY),
        (ServiceError(400, "LimitExceeded", {}, "limit"), ErrorKind.QUOTA_EXCEEDED),
        (ServiceError(409, "QuotaExceeded", {}, "quota"), ErrorKind.QUOTA_EXCEEDED),
        (ServiceError(429, "TooManyRequests", {}, "slow down"), ErrorKind.RATE_LIMITED),
        (ServiceError(404, "NotAuthorizedOrNotFound", {}, "nope"), ErrorKind.AUTH),
        (ServiceError(500, "InternalError", {}, "unrelated failure"), ErrorKind.OTHER),
    ]
    for error, expected in cases:
        actual = classify(error)
        check(
            f"{error.status} {error.code} -> {expected.value}",
            actual is expected,
            f"(got {actual.value})",
        )
    check("quota is the only fatal kind", ErrorKind.QUOTA_EXCEEDED.is_fatal)
    check("capacity is not fatal", not ErrorKind.OUT_OF_CAPACITY.is_fatal)


def test_payload(cfg) -> None:
    print("\n[2] launch payload")
    details = build_launch_details(cfg, ADS[0], "ssh-ed25519 AAAA test@ci")
    check("availability domain set", details.availability_domain == ADS[0])
    check("shape is A1.Flex", details.shape == "VM.Standard.A1.Flex")
    check("ocpus", details.shape_config.ocpus == 2)
    check("memory", details.shape_config.memory_in_gbs == 12)
    check("boot volume", details.source_details.boot_volume_size_in_gbs == 50)
    check("public ip requested", details.create_vnic_details.assign_public_ip is True)
    check("ssh key in metadata", "ssh-ed25519" in details.metadata["ssh_authorized_keys"])


def test_retry_then_success(cfg) -> None:
    print("\n[3] capacity errors rotate ADs, then succeed")
    code, calls = run_launcher(cfg, [CAPACITY_ERROR] * 4)
    check("exit code 0", code == 0, f"(got {code})")
    check("gave up on 4 ADs before winning on the 5th", calls == 5, f"(got {calls})")


def test_quota_aborts(cfg) -> None:
    print("\n[4] quota exceeded aborts immediately")
    script = [CAPACITY_ERROR, ServiceError(400, "LimitExceeded", {}, "a1-core-count")]
    code, calls = run_launcher(cfg, script)
    check("exit code 1", code == 1, f"(got {code})")
    check("stopped at the quota error", calls == 2, f"(got {calls})")


def test_max_rounds(cfg) -> None:
    print("\n[5] MAX_ROUNDS stops the loop")
    original = cfg.max_rounds
    cfg.max_rounds = 2
    try:
        code, calls = run_launcher(cfg, [CAPACITY_ERROR] * 50)
    finally:
        cfg.max_rounds = original
    check("exit code 1", code == 1, f"(got {code})")
    check("tried every AD twice", calls == len(ADS) * 2, f"(got {calls})")


def test_dry_run(cfg) -> None:
    print("\n[6] dry run never launches")
    code, _ = run_launcher(cfg, [], dry_run=True)
    check("exit code 0", code == 0, f"(got {code})")


def test_shutdown_interrupts_sleep() -> None:
    print("\n[7] shutdown interrupts a long sleep")
    import time

    shutdown = Shutdown()
    shutdown._event.set()  # simulate a signal that already arrived
    started = time.monotonic()
    completed = shutdown.sleep(30)
    elapsed = time.monotonic() - started
    check("sleep returned False", completed is False)
    check("returned immediately", elapsed < 1.0, f"(took {elapsed:.2f}s)")


def main() -> int:
    setup_logging("WARNING", None)
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        env_path = write_fixture_env(directory)
        cfg = load_fast_config(env_path)

        test_error_classification()
        test_payload(cfg)
        test_retry_then_success(cfg)
        test_quota_aborts(cfg)
        test_max_rounds(cfg)
        test_dry_run(cfg)
        test_shutdown_interrupts_sleep()

    print("\n" + "-" * 60)
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("All smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
