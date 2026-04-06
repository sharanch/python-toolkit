"""
disk_monitor.py — SRE Disk Usage Monitor
Checks disk usage and sends alerts when thresholds are exceeded.
Includes cooldown logic to prevent alert fatigue.

Usage:
    python disk_monitor.py
    python disk_monitor.py --threshold 80
"""

import sys
import os
import json
import logging
import subprocess
import argparse
from datetime import datetime, timedelta

# ── logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# ── config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "../config/config.json")


def load_config():
    """Load configuration from config.json."""
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning("config.json not found, using defaults")
        return {"disk": {"threshold_percent": 50, "cooldown_hours": 1,
                         "state_file": "/tmp/disk_alert_state.json"}}
    except json.JSONDecodeError as e:
        logger.error(f"Invalid config.json: {e}")
        sys.exit(1)


# ── alert cooldown ────────────────────────────────────────────────────────────
def should_send_alert(mountpoint, state_file, cooldown_hours):
    """
    Check if enough time has passed since the last alert for this mountpoint.

    Args:
        mountpoint (str): Disk mountpoint e.g. "/" or "/data"
        state_file (str): Path to JSON file tracking last alert times
        cooldown_hours (int): Hours to wait before re-alerting

    Returns:
        bool: True if alert should be sent, False if still in cooldown
    """
    try:
        if os.path.exists(state_file):
            with open(state_file, "r") as f:
                state = json.load(f)
        else:
            state = {}

        now = datetime.now()
        last_sent = state.get(mountpoint)

        if last_sent:
            last_sent_time = datetime.fromisoformat(last_sent)
            if now - last_sent_time < timedelta(hours=cooldown_hours):
                logger.info(f"Alert suppressed for {mountpoint} — still in cooldown")
                return False

        # update state
        state[mountpoint] = now.isoformat()
        with open(state_file, "w") as f:
            json.dump(state, f, indent=2)

        return True

    except (json.JSONDecodeError, PermissionError) as e:
        logger.warning(f"Could not read/write state file: {e}, sending alert anyway")
        return True


# ── disk check ────────────────────────────────────────────────────────────────
def get_disk_usage():
    """
    Run df -h and return parsed disk usage data.

    Returns:
        list of dicts: Each dict has filesystem, size, used, available, percent, mountpoint
    """
    try:
        result = subprocess.run(
            ["df", "-h"],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            logger.error(f"df command failed: {result.stderr}")
            sys.exit(1)

        disks = []
        lines = result.stdout.splitlines()

        for line in lines[1:]:   # skip header
            parts = line.split()
            if len(parts) < 6:
                continue
            try:
                disks.append({
                    "filesystem": parts[0],
                    "size":       parts[1],
                    "used":       parts[2],
                    "available":  parts[3],
                    "percent":    int(parts[4].strip("%")),
                    "mountpoint": parts[5]
                })
            except ValueError:
                logger.warning(f"Skipping malformed df line: {line}")
                continue

        return disks

    except FileNotFoundError:
        logger.error("df command not found — is this a Linux system?")
        sys.exit(1)


def check_disks(config, threshold_override=None):
    """
    Check all disks and alert on any exceeding the threshold.

    Args:
        config (dict): Full config dict
        threshold_override (int): Optional CLI threshold override
    """
    disk_config    = config.get("disk", {})
    threshold      = threshold_override or disk_config.get("threshold_percent", 50)
    cooldown_hours = disk_config.get("cooldown_hours", 1)
    state_file     = disk_config.get("state_file", "/tmp/disk_alert_state.json")

    logger.info(f"Checking disk usage (threshold: {threshold}%)")
    disks = get_disk_usage()

    high_usage = [d for d in disks if d["percent"] > threshold]

    if not high_usage:
        print(f"✓ All disks below {threshold}% — healthy")
        sys.exit(0)

    print(f"\n=== High Disk Usage (>{threshold}%) ===")
    for disk in high_usage:
        print(f"  {disk['mountpoint']}: {disk['percent']}% "
              f"(used {disk['used']} of {disk['size']})")

        if should_send_alert(disk["mountpoint"], state_file, cooldown_hours):
            subject = f"ALERT: High disk usage on {disk['mountpoint']}"
            body = (f"Mountpoint: {disk['mountpoint']}\n"
                    f"Usage: {disk['percent']}%\n"
                    f"Used: {disk['used']} of {disk['size']}\n"
                    f"Available: {disk['available']}")

            # import here to avoid circular imports
            from alert import send_alert
            send_alert(config, subject, body)
            logger.info(f"Alert sent for {disk['mountpoint']}")
        else:
            logger.info(f"Alert skipped for {disk['mountpoint']} (cooldown)")

    print()
    sys.exit(1)   # non-zero exit so cron/monitoring knows something is wrong


# ── entrypoint ────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="SRE Disk Usage Monitor")
    parser.add_argument("--threshold", type=int, help="Override disk usage threshold (%)")
    args = parser.parse_args()

    config = load_config()
    check_disks(config, threshold_override=args.threshold)


if __name__ == "__main__":
    main()
