#!/usr/bin/env python3
"""
process_monitor.py

Monitor CPU and memory usage of specific processes by name or PID.
Alerts when thresholds are exceeded. Supports repeated sampling with
a configurable interval for smoothing out CPU spikes.

Usage:
    python scripts/process_monitor.py --processes nginx postgres
    python scripts/process_monitor.py --processes nginx --cpu-threshold 80 --mem-threshold 1024
    python scripts/process_monitor.py --pid 1234 --samples 3 --interval 2
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

try:
    import psutil
except ImportError:
    print("ERROR: psutil is required — run: pip install psutil")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.alert import send_slack_alert, send_email_alert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.json"

DEFAULT_CPU_THRESHOLD = 80.0    # percent
DEFAULT_MEM_THRESHOLD_MB = 512  # MB RSS
DEFAULT_SAMPLES = 3             # number of CPU samples to average
DEFAULT_INTERVAL = 1.0          # seconds between samples


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def find_processes(name: str) -> list[psutil.Process]:
    """Find all running processes matching the given name (case-insensitive)."""
    matches = []
    for proc in psutil.process_iter(["pid", "name", "status"]):
        try:
            if name.lower() in proc.info["name"].lower():
                matches.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return matches


def sample_process(proc: psutil.Process, samples: int, interval: float) -> dict | None:
    """
    Sample a process's CPU and memory over multiple readings.
    Returns averaged stats, or None if the process died mid-sampling.
    """
    cpu_readings = []
    mem_mb = None

    try:
        # First call to cpu_percent seeds the counter; subsequent calls measure real usage
        proc.cpu_percent(interval=None)
        for i in range(samples):
            time.sleep(interval)
            cpu_readings.append(proc.cpu_percent(interval=None))

        mem_info = proc.memory_info()
        mem_mb = mem_info.rss / (1024 * 1024)

        return {
            "pid": proc.pid,
            "name": proc.name(),
            "status": proc.status(),
            "cpu_percent": round(sum(cpu_readings) / len(cpu_readings), 2),
            "mem_mb": round(mem_mb, 2),
            "cpu_readings": cpu_readings,
        }

    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        log.warning("PID %d vanished or access denied during sampling: %s", proc.pid, e)
        return None


def check_process_group(
    name: str,
    procs: list[psutil.Process],
    cpu_threshold: float,
    mem_threshold_mb: float,
    samples: int,
    interval: float,
) -> dict:
    """
    Sample all instances of a process name, aggregate, and evaluate against thresholds.
    """
    if not procs:
        log.warning("Process not found: %s", name)
        return {
            "name": name,
            "status": "not_found",
            "instances": [],
            "total_cpu": 0,
            "total_mem_mb": 0,
            "breaches": ["Process not running"],
        }

    log.info("Sampling %d instance(s) of '%s' (%d samples @ %.1fs interval)...",
             len(procs), name, samples, interval)

    stats = [s for proc in procs if (s := sample_process(proc, samples, interval))]

    if not stats:
        return {
            "name": name,
            "status": "error",
            "instances": [],
            "total_cpu": 0,
            "total_mem_mb": 0,
            "breaches": ["All instances vanished during sampling"],
        }

    total_cpu = round(sum(s["cpu_percent"] for s in stats), 2)
    total_mem = round(sum(s["mem_mb"] for s in stats), 2)

    for s in stats:
        log.info(
            "  PID %-7d  CPU: %5.1f%%  MEM: %7.1f MB  [%s]",
            s["pid"], s["cpu_percent"], s["mem_mb"], s["status"]
        )

    breaches = []
    if total_cpu > cpu_threshold:
        breaches.append(f"Total CPU {total_cpu}% exceeds threshold {cpu_threshold}%")
    if total_mem > mem_threshold_mb:
        breaches.append(f"Total MEM {total_mem:.1f} MB exceeds threshold {mem_threshold_mb:.1f} MB")

    status = "critical" if breaches else "ok"
    log.info(
        "  => Total CPU: %.1f%%  Total MEM: %.1f MB  [%s]",
        total_cpu, total_mem, status.upper()
    )

    return {
        "name": name,
        "status": status,
        "instances": stats,
        "total_cpu": total_cpu,
        "total_mem_mb": total_mem,
        "breaches": breaches,
    }


def build_alert_message(results: list[dict], cpu_threshold: float, mem_threshold_mb: float) -> str:
    lines = [
        "*Process Monitor Alert*",
        f"Thresholds — CPU: {cpu_threshold}%  MEM: {mem_threshold_mb:.0f} MB\n",
    ]

    for r in results:
        if r["status"] == "not_found":
            lines.append(f":ghost: *{r['name']}* — process not found")
        elif r["status"] == "error":
            lines.append(f":x: *{r['name']}* — sampling error")
        elif r["status"] == "critical":
            lines.append(f":rotating_light: *{r['name']}* — {len(r['instances'])} instance(s)")
            lines.append(f"  Total CPU: {r['total_cpu']}%  |  Total MEM: {r['total_mem_mb']:.1f} MB")
            for breach in r["breaches"]:
                lines.append(f"  • {breach}")
            for inst in r["instances"]:
                lines.append(
                    f"  PID {inst['pid']}: CPU {inst['cpu_percent']}%  MEM {inst['mem_mb']:.1f} MB"
                )

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Monitor CPU/memory of specific processes")
    parser.add_argument("--processes", nargs="+", help="Process names to monitor")
    parser.add_argument("--pid", type=int, help="Monitor a specific PID")
    parser.add_argument(
        "--cpu-threshold",
        type=float,
        default=DEFAULT_CPU_THRESHOLD,
        help=f"CPU alert threshold %% (default: {DEFAULT_CPU_THRESHOLD})",
    )
    parser.add_argument(
        "--mem-threshold",
        type=float,
        default=DEFAULT_MEM_THRESHOLD_MB,
        help=f"Memory alert threshold MB (default: {DEFAULT_MEM_THRESHOLD_MB})",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=DEFAULT_SAMPLES,
        help=f"CPU samples to average (default: {DEFAULT_SAMPLES})",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help=f"Seconds between samples (default: {DEFAULT_INTERVAL})",
    )
    parser.add_argument("--no-alert", action="store_true", help="Print results only, no alerts")
    args = parser.parse_args()

    config = load_config()
    proc_config = config.get("process_monitor", {})
    alerts_config = config.get("alerts", {})

    cpu_threshold = args.cpu_threshold or proc_config.get("cpu_threshold", DEFAULT_CPU_THRESHOLD)
    mem_threshold = args.mem_threshold or proc_config.get("mem_threshold_mb", DEFAULT_MEM_THRESHOLD_MB)

    results = []

    # Single PID mode
    if args.pid:
        try:
            proc = psutil.Process(args.pid)
            name = proc.name()
            result = check_process_group(name, [proc], cpu_threshold, mem_threshold, args.samples, args.interval)
            results.append(result)
        except psutil.NoSuchProcess:
            log.error("No process with PID %d", args.pid)
            sys.exit(1)

    else:
        process_names = args.processes or proc_config.get("processes", [])
        if not process_names:
            log.error("No processes specified. Use --processes or set process_monitor.processes in config.json")
            sys.exit(1)

        for name in process_names:
            procs = find_processes(name)
            result = check_process_group(name, procs, cpu_threshold, mem_threshold, args.samples, args.interval)
            results.append(result)

    # Summary
    issues = [r for r in results if r["status"] != "ok"]
    log.info("Summary — %d process group(s) checked, %d with issues", len(results), len(issues))

    if issues and not args.no_alert:
        message = build_alert_message(issues, cpu_threshold, mem_threshold)
        slack_url = alerts_config.get("slack_webhook_url")
        if slack_url:
            send_slack_alert(message, slack_url)
        smtp_user = alerts_config.get("smtp_user")
        if smtp_user:
            send_email_alert(
                subject="[PROCESS ALERT] CPU/Memory threshold exceeded",
                body=message,
                config=alerts_config,
            )

    sys.exit(1 if issues else 0)


if __name__ == "__main__":
    main()
