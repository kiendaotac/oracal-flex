"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from . import __version__
from .config import load_config
from .launcher import Launcher
from .logging_setup import setup_logging
from .oci_clients import build_clients
from .preflight import report, run_preflight
from .shutdown import Shutdown

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="a1launcher",
        description=(
            "Retry launching an Oracle Cloud VM.Standard.A1.Flex (Always Free) instance "
            "until capacity is available."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"a1launcher {__version__}")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run preflight checks and exit without launching anything.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the launch payload for every AD but never call LaunchInstance.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Path to the .env file.",
    )
    parser.add_argument(
        "--config",
        dest="yaml_file",
        type=Path,
        default=Path("config.yaml"),
        help="Path to config.yaml.",
    )
    parser.add_argument("--profile", help="Override OCI_PROFILE from the config.")
    parser.add_argument("--log-file", type=Path, help="Override LOG_FILE from the config.")
    parser.add_argument("--log-level", help="Override LOG_LEVEL (DEBUG, INFO, WARNING, ERROR).")
    parser.add_argument(
        "--no-api-check",
        action="store_true",
        help="Skip the read-only API call during preflight (offline config validation).",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Launch without running preflight first. Not recommended.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    cfg, config_errors = load_config(args.env_file, args.yaml_file)
    if args.profile:
        cfg.oci_profile = args.profile
    if args.log_file:
        cfg.log_file = args.log_file
    if args.log_level:
        cfg.log_level = args.log_level.upper()

    logger = setup_logging(
        level=cfg.log_level,
        log_file=cfg.log_file,
        max_bytes=cfg.log_max_bytes,
        backup_count=cfg.log_backup_count,
    )
    logger.info("a1launcher %s starting (mode=%s)", __version__, _mode(args))

    # ------------------------------------------------------------------ #
    # Preflight
    # ------------------------------------------------------------------ #
    if args.check or not args.skip_preflight:
        results = run_preflight(
            cfg,
            config_errors,
            env_file=args.env_file,
            yaml_file=args.yaml_file,
            skip_api=args.no_api_check,
        )
        passed = report(results)
        if args.check:
            return EXIT_OK if passed else EXIT_ERROR
        if not passed:
            logger.error("Preflight failed — refusing to launch. Fix the FAIL items above.")
            return EXIT_ERROR
    elif config_errors:
        for error in config_errors:
            logger.error("Config error: %s", error)
        return EXIT_ERROR

    # ------------------------------------------------------------------ #
    # Launch
    # ------------------------------------------------------------------ #
    shutdown = Shutdown()
    shutdown.install()

    try:
        clients = build_clients(cfg)
    except Exception:  # noqa: BLE001 - surfaced with a full traceback below
        logger.error("Could not build OCI clients:\n%s", traceback.format_exc())
        return EXIT_ERROR

    launcher = Launcher(cfg, clients, shutdown, dry_run=args.dry_run)
    try:
        return launcher.run()
    except KeyboardInterrupt:  # pragma: no cover - handler normally catches this first
        logger.info("Interrupted")
        return EXIT_INTERRUPTED
    except Exception:  # noqa: BLE001
        logger.error("Fatal error:\n%s", traceback.format_exc())
        return EXIT_ERROR


def _mode(args: argparse.Namespace) -> str:
    if args.check:
        return "check"
    return "dry-run" if args.dry_run else "launch"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
