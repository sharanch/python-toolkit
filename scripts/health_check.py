"""
health_check.py — SRE Service Health Checker
Checks HTTP health endpoints and reports service status.
Sends alerts for services that are DOWN or slow.

Usage:
    python health_check.py
    python health_check.py --url https://myservice.com/health
    python health_check.py --timeout 10
"""

import sys
import os
import json
import logging
import argparse

try:
    import requests
except ImportError:
    print("ERROR: requests library not installed. Run: pip install requests")
    sys.exit(1)

# ── logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# ── config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "../config/config.json")

# response time threshold — warn if slower than this
SLOW_RESPONSE_MS = 1000


def load_config():
    """Load configuration from config.json."""
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning("config.json not found, using defaults")
        return {
            "health_check": {
                "timeout_seconds": 5,
                "services": []
            }
        }
    except json.JSONDecodeError as e:
        logger.error(f"Invalid config.json: {e}")
        sys.exit(1)


# ── health check logic ────────────────────────────────────────────────────────
def check_service(name, url, timeout):
    """
    Perform a single HTTP health check.

    Args:
        name (str): Human readable service name
        url (str): URL to check
        timeout (int): Request timeout in seconds

    Returns:
        dict: Result with status, status_code, response_ms, error
    """
    try:
        response = requests.get(url, timeout=timeout)
        response_ms = int(response.elapsed.total_seconds() * 1000)
        is_up = 200 <= response.status_code < 300

        return {
            "name":        name,
            "url":         url,
            "status":      "UP" if is_up else "DOWN",
            "status_code": response.status_code,
            "response_ms": response_ms,
            "error":       None
        }

    except requests.exceptions.ConnectionError:
        return {
            "name":        name,
            "url":         url,
            "status":      "DOWN",
            "status_code": None,
            "response_ms": None,
            "error":       "Connection refused or DNS failure"
        }
    except requests.exceptions.Timeout:
        return {
            "name":        name,
            "url":         url,
            "status":      "DOWN",
            "status_code": None,
            "response_ms": None,
            "error":       f"Timed out after {timeout}s"
        }
    except requests.exceptions.SSLError:
        return {
            "name":        name,
            "url":         url,
            "status":      "DOWN",
            "status_code": None,
            "response_ms": None,
            "error":       "SSL certificate error"
        }
    except Exception as e:
        return {
            "name":        name,
            "url":         url,
            "status":      "DOWN",
            "status_code": None,
            "response_ms": None,
            "error":       str(e)
        }


def print_report(results):
    """Print a formatted health check report."""
    print("\n=== Service Health Report ===\n")

    for r in results:
        status_icon = "✓" if r["status"] == "UP" else "✗"

        if r["status"] == "UP":
            speed = ""
            if r["response_ms"] and r["response_ms"] > SLOW_RESPONSE_MS:
                speed = f" ⚠ SLOW ({r['response_ms']}ms)"
            else:
                speed = f" ({r['response_ms']}ms)"
            print(f"  {status_icon} {r['name']}: UP [{r['status_code']}]{speed}")
        else:
            error = r["error"] or f"HTTP {r['status_code']}"
            print(f"  {status_icon} {r['name']}: DOWN — {error}")

    total  = len(results)
    up     = sum(1 for r in results if r["status"] == "UP")
    down   = total - up

    print(f"\n  {up}/{total} services healthy")
    if down > 0:
        print(f"  {down} service(s) DOWN\n")
    else:
        print()


def run_health_checks(config, url_override=None, timeout_override=None):
    """
    Run health checks for all configured services.

    Args:
        config (dict): Full config dict
        url_override (str): Optional single URL to check from CLI
        timeout_override (int): Optional timeout override from CLI
    """
    hc_config = config.get("health_check", {})
    timeout   = timeout_override or hc_config.get("timeout_seconds", 5)

    # if a single URL is passed via CLI, check just that
    if url_override:
        services = [{"name": url_override, "url": url_override}]
    else:
        services = hc_config.get("services", [])

    if not services:
        logger.error("No services configured. Add services to config.json or use --url")
        sys.exit(1)

    logger.info(f"Checking {len(services)} service(s) with timeout={timeout}s")

    results = []
    for service in services:
        logger.info(f"Checking {service['name']}...")
        result = check_service(service["name"], service["url"], timeout)
        results.append(result)

    print_report(results)

    # send alerts for DOWN services
    down_services = [r for r in results if r["status"] == "DOWN"]
    if down_services:
        from alert import send_alert
        for r in down_services:
            subject = f"ALERT: {r['name']} is DOWN"
            body    = (f"Service: {r['name']}\n"
                       f"URL: {r['url']}\n"
                       f"Error: {r['error'] or f'HTTP {r[\"status_code\"]}'}")
            send_alert(config, subject, body)

        sys.exit(1)   # non-zero so cron/monitoring knows something is wrong

    sys.exit(0)


# ── entrypoint ────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="SRE Service Health Checker")
    parser.add_argument("--url",     type=str, help="Check a single URL")
    parser.add_argument("--timeout", type=int, help="Request timeout in seconds")
    args = parser.parse_args()

    config = load_config()
    run_health_checks(config, url_override=args.url, timeout_override=args.timeout)


if __name__ == "__main__":
    main()
