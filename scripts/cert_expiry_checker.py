#!/usr/bin/env python3
"""
cert_expiry_checker.py

Check TLS certificate expiry for a list of domains.
Alerts via Slack/email when a cert is expiring within the warning threshold.

Usage:
    python scripts/cert_expiry_checker.py
    python scripts/cert_expiry_checker.py --domains example.com api.example.com
    python scripts/cert_expiry_checker.py --warn-days 30 --critical-days 7
"""

import argparse
import json
import logging
import socket
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running from repo root or scripts/ dir
sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.alert import send_slack_alert, send_email_alert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.json"

DEFAULT_WARN_DAYS = 30
DEFAULT_CRITICAL_DAYS = 7
DEFAULT_PORT = 443
DEFAULT_TIMEOUT = 10


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def get_cert_expiry(hostname: str, port: int = DEFAULT_PORT, timeout: int = DEFAULT_TIMEOUT) -> datetime:
    """
    Open a TLS connection to hostname:port and return the certificate's
    notAfter datetime (UTC-aware).
    """
    ctx = ssl.create_default_context()
    with socket.create_connection((hostname, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
            cert = ssock.getpeercert()

    # notAfter format: 'Apr  6 12:00:00 2026 GMT'
    expiry_str = cert["notAfter"]
    expiry_dt = datetime.strptime(expiry_str, "%b %d %H:%M:%S %Y %Z")
    return expiry_dt.replace(tzinfo=timezone.utc)


def check_domain(hostname: str, port: int, warn_days: int, critical_days: int) -> dict:
    """
    Check a single domain. Returns a result dict with status, days_remaining, etc.
    """
    result = {
        "hostname": hostname,
        "port": port,
        "status": "ok",
        "days_remaining": None,
        "expiry": None,
        "error": None,
    }

    try:
        expiry_dt = get_cert_expiry(hostname, port)
        now = datetime.now(tz=timezone.utc)
        days_remaining = (expiry_dt - now).days

        result["expiry"] = expiry_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        result["days_remaining"] = days_remaining

        if days_remaining <= critical_days:
            result["status"] = "critical"
        elif days_remaining <= warn_days:
            result["status"] = "warning"
        else:
            result["status"] = "ok"

        log.info(
            "%-40s  expires: %s  (%d days)  [%s]",
            f"{hostname}:{port}",
            result["expiry"],
            days_remaining,
            result["status"].upper(),
        )

    except ssl.SSLCertVerificationError as e:
        result["status"] = "critical"
        result["error"] = f"SSL verification failed: {e}"
        log.error("%s — SSL verification failed: %s", hostname, e)

    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        result["status"] = "error"
        result["error"] = f"Connection error: {e}"
        log.error("%s — connection error: %s", hostname, e)

    return result


def build_alert_message(results: list[dict], warn_days: int, critical_days: int) -> str:
    critical = [r for r in results if r["status"] == "critical"]
    warning = [r for r in results if r["status"] == "warning"]
    errors = [r for r in results if r["status"] == "error"]

    lines = ["*TLS Certificate Expiry Report*\n"]

    if critical:
        lines.append(f":rotating_light: *CRITICAL* (≤{critical_days} days)")
        for r in critical:
            if r["error"]:
                lines.append(f"  • `{r['hostname']}:{r['port']}` — {r['error']}")
            else:
                lines.append(f"  • `{r['hostname']}:{r['port']}` — expires {r['expiry']} ({r['days_remaining']} days)")

    if warning:
        lines.append(f"\n:warning: *WARNING* (≤{warn_days} days)")
        for r in warning:
            lines.append(f"  • `{r['hostname']}:{r['port']}` — expires {r['expiry']} ({r['days_remaining']} days)")

    if errors:
        lines.append("\n:x: *CONNECTION ERRORS*")
        for r in errors:
            lines.append(f"  • `{r['hostname']}:{r['port']}` — {r['error']}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="TLS certificate expiry checker")
    parser.add_argument(
        "--domains",
        nargs="+",
        help="Domains to check (overrides config). Format: hostname or hostname:port",
    )
    parser.add_argument(
        "--warn-days",
        type=int,
        default=DEFAULT_WARN_DAYS,
        help=f"Warn when cert expires within N days (default: {DEFAULT_WARN_DAYS})",
    )
    parser.add_argument(
        "--critical-days",
        type=int,
        default=DEFAULT_CRITICAL_DAYS,
        help=f"Critical when cert expires within N days (default: {DEFAULT_CRITICAL_DAYS})",
    )
    parser.add_argument(
        "--no-alert",
        action="store_true",
        help="Print results only, do not send alerts",
    )
    args = parser.parse_args()

    config = load_config()
    cert_config = config.get("cert_checker", {})
    alerts_config = config.get("alerts", {})

    # Build domain list: CLI args take precedence over config
    domains_raw = args.domains or cert_config.get("domains", [])
    if not domains_raw:
        log.error("No domains specified. Pass --domains or set cert_checker.domains in config.json")
        sys.exit(1)

    # Parse optional :port suffix
    targets = []
    for entry in domains_raw:
        if ":" in entry:
            host, port_str = entry.rsplit(":", 1)
            targets.append((host, int(port_str)))
        else:
            targets.append((entry, DEFAULT_PORT))

    warn_days = args.warn_days or cert_config.get("warn_days", DEFAULT_WARN_DAYS)
    critical_days = args.critical_days or cert_config.get("critical_days", DEFAULT_CRITICAL_DAYS)

    log.info("Checking %d domain(s) — warn=%dd  critical=%dd", len(targets), warn_days, critical_days)

    results = [check_domain(host, port, warn_days, critical_days) for host, port in targets]

    # Summary
    ok = sum(1 for r in results if r["status"] == "ok")
    warn = sum(1 for r in results if r["status"] == "warning")
    crit = sum(1 for r in results if r["status"] in ("critical", "error"))
    log.info("Summary — OK: %d  WARNING: %d  CRITICAL/ERROR: %d", ok, warn, crit)

    # Alert if any non-ok results
    needs_alert = any(r["status"] != "ok" for r in results)
    if needs_alert and not args.no_alert:
        message = build_alert_message(results, warn_days, critical_days)
        slack_url = alerts_config.get("slack_webhook_url")
        if slack_url:
            send_slack_alert(message, slack_url)
        else:
            log.warning("No Slack webhook configured — skipping Slack alert")

        smtp_user = alerts_config.get("smtp_user")
        if smtp_user:
            send_email_alert(
                subject="[CERT ALERT] TLS certificate expiry warning",
                body=message,
                config=alerts_config,
            )

    # Exit code reflects worst status for use in cron/CI
    if crit:
        sys.exit(2)
    if warn:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
