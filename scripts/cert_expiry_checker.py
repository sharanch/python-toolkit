#!/usr/bin/env python3
"""
cert_expiry_checker.py — SRE TLS Certificate Expiry Checker
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
import os
import socket
import ssl
import sys
from datetime import datetime, timezone

# ── logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "../config/config.json")

DEFAULT_WARN_DAYS     = 30
DEFAULT_CRITICAL_DAYS = 7
DEFAULT_PORT          = 443
DEFAULT_TIMEOUT       = 10


def load_config():
    """Load configuration from config.json."""
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning("config.json not found, using defaults")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"Invalid config.json: {e}")
        sys.exit(1)


# ── core logic ────────────────────────────────────────────────────────────────
def get_cert_expiry(hostname, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
    """
    Open a TLS connection to hostname:port and return the certificate's
    notAfter datetime (UTC-aware).

    Args:
        hostname (str): Domain to check
        port (int): Port to connect on
        timeout (int): Connection timeout in seconds

    Returns:
        datetime: UTC-aware expiry datetime
    """
    ctx = ssl.create_default_context()
    with socket.create_connection((hostname, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
            cert = ssock.getpeercert()

    # notAfter format: 'Apr  6 12:00:00 2026 GMT'
    expiry_str = cert["notAfter"]
    expiry_dt  = datetime.strptime(expiry_str, "%b %d %H:%M:%S %Y %Z")
    return expiry_dt.replace(tzinfo=timezone.utc)


def check_domain(hostname, port, warn_days, critical_days):
    """
    Check a single domain's certificate expiry.

    Args:
        hostname (str): Domain to check
        port (int): TLS port
        warn_days (int): Days before expiry to warn
        critical_days (int): Days before expiry to go critical

    Returns:
        dict: Result with status, days_remaining, expiry, error
    """
    result = {
        "hostname":       hostname,
        "port":           port,
        "status":         "ok",
        "days_remaining": None,
        "expiry":         None,
        "error":          None,
    }

    try:
        expiry_dt      = get_cert_expiry(hostname, port)
        now            = datetime.now(tz=timezone.utc)
        days_remaining = (expiry_dt - now).days

        result["expiry"]         = expiry_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        result["days_remaining"] = days_remaining

        if days_remaining <= critical_days:
            result["status"] = "critical"
        elif days_remaining <= warn_days:
            result["status"] = "warning"

        logger.info(
            "%-40s  expires: %s  (%d days)  [%s]",
            f"{hostname}:{port}",
            result["expiry"],
            days_remaining,
            result["status"].upper(),
        )

    except ssl.SSLCertVerificationError as e:
        result["status"] = "critical"
        result["error"]  = f"SSL verification failed: {e}"
        logger.error(f"{hostname} — SSL verification failed: {e}")

    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        result["status"] = "error"
        result["error"]  = f"Connection error: {e}"
        logger.error(f"{hostname} — connection error: {e}")

    return result


def build_alert_message(results, warn_days, critical_days):
    """Build a formatted Slack/email alert message from results."""
    critical = [r for r in results if r["status"] == "critical"]
    warning  = [r for r in results if r["status"] == "warning"]
    errors   = [r for r in results if r["status"] == "error"]

    lines = ["*TLS Certificate Expiry Report*\n"]

    if critical:
        lines.append(f":rotating_light: *CRITICAL* (≤{critical_days} days)")
        for r in critical:
            if r["error"]:
                lines.append(f"  • `{r['hostname']}:{r['port']}` — {r['error']}")
            else:
                lines.append(
                    f"  • `{r['hostname']}:{r['port']}` — "
                    f"expires {r['expiry']} ({r['days_remaining']} days)"
                )

    if warning:
        lines.append(f"\n:warning: *WARNING* (≤{warn_days} days)")
        for r in warning:
            lines.append(
                f"  • `{r['hostname']}:{r['port']}` — "
                f"expires {r['expiry']} ({r['days_remaining']} days)"
            )

    if errors:
        lines.append("\n:x: *CONNECTION ERRORS*")
        for r in errors:
            lines.append(f"  • `{r['hostname']}:{r['port']}` — {r['error']}")

    return "\n".join(lines)


# ── entrypoint ────────────────────────────────────────────────────────────────
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

    config      = load_config()
    cert_config = config.get("cert_checker", {})

    # CLI args take precedence over config
    domains_raw   = args.domains or cert_config.get("domains", [])
    warn_days     = args.warn_days     or cert_config.get("warn_days",     DEFAULT_WARN_DAYS)
    critical_days = args.critical_days or cert_config.get("critical_days", DEFAULT_CRITICAL_DAYS)

    if not domains_raw:
        logger.error("No domains specified. Pass --domains or set cert_checker.domains in config.json")
        sys.exit(1)

    # parse optional :port suffix
    targets = []
    for entry in domains_raw:
        if ":" in entry:
            host, port_str = entry.rsplit(":", 1)
            targets.append((host, int(port_str)))
        else:
            targets.append((entry, DEFAULT_PORT))

    logger.info(f"Checking {len(targets)} domain(s) — warn={warn_days}d  critical={critical_days}d")

    results = [check_domain(host, port, warn_days, critical_days) for host, port in targets]

    ok   = sum(1 for r in results if r["status"] == "ok")
    warn = sum(1 for r in results if r["status"] == "warning")
    crit = sum(1 for r in results if r["status"] in ("critical", "error"))
    logger.info(f"Summary — OK: {ok}  WARNING: {warn}  CRITICAL/ERROR: {crit}")

    needs_alert = any(r["status"] != "ok" for r in results)
    if needs_alert and not args.no_alert:
        message = build_alert_message(results, warn_days, critical_days)
        from alert import send_alert
        send_alert(config, "[CERT ALERT] TLS certificate expiry warning", message)

    if crit:
        sys.exit(2)
    if warn:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()