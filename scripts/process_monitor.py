#!/usr/bin/env python3
"""
process_monitor.py — SRE Process Monitor
Monitor CPU and memory usage of specific processes by name or PID.
Alerts when thresholds are exceeded. Supports repeated sampling to
smooth out CPU spikes.

Requires: pip install psutil

Usage:
    python scripts/process_monitor.py --processes nginx postgres
    python scripts/process_monitor.py --processes nginx --cpu-threshold 80 --mem-threshold 1024
    python scripts/process_monitor.py --pid 1234 --samples 3 --interval 2
"""

import argparse
import json
import logging
import os
import sys
import time

try:
    import psutil
except ImportError:
    print("ERROR: psutil is required — run: pip install psutil")
    sys.exit(1)

# ── logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── config ────────────────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "../config/config.json")

DEFAULT_CPU_THRESHOLD    = 80.0   # percent
DEFAULT_MEM_THRESHOLD_MB = 512    # MB RSS
DEFAULT_SAMPLES          = 3      # number of CPU samples to average
DEFAULT_INTERVAL         = 1.0    # seconds between samples


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
def find_processes(name):
    """
    Find all running processes matching the given name (case-insensitive).

    Args:
        name (str): Process name to search for

    Returns:
        list: Matching psutil.Process objects
    """
    matches = []
    for proc in psutil.process_iter(["pid", "name", "status"]):
        try:
            if name.lower() in proc.info["name"].lower():
                matches.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return matches


def sample_process(proc, samples, interval):
    """
    Sample a process's CPU and memory over multiple readings.

    Args:
        proc (psutil.Process): Process to sample
        samples (int): Number of CPU readings to take
        interval (float): Seconds between readings

    Returns:
        dict or None: Averaged stats, or None if process died mid-sampling
    """
    cpu_readings = []

    try:
        # first call seeds the counter; subsequent calls measure real usage
        proc.cpu_percent(interval=None)

        for _ in range(samples):
            time.sleep(interval)
            cpu_readings.append(proc.cpu_percent(interval=None))

        mem_info = proc.memory_info()
        mem_mb   = mem_info.rss / (1024 * 1024)

        return {
            "pid":          proc.pid,
            "name":         proc.name(),
            "status":       proc.status(),
            "cpu_percent":  round(sum(cpu_readings) / len(cpu_readings), 2),
            "mem_mb":       round(mem_mb, 2),
        }

    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
        logger.warning(f"PID {proc.pid} vanished or access denied during sampling: {e}")
        return None


def check_process_group(name, procs, cpu_threshold, mem_threshold_mb, samples, interval):
    """
    Sample all instances of a process name and evaluate against thresholds.

    Args:
        name (str): Process name label
        procs (list): psutil.Process instances to check
        cpu_threshold (float): CPU % threshold
        mem_threshold_mb (float): Memory MB threshold
        samples (int): CPU sample count
        interval (float): Seconds between samples

    Returns:
        dict: Result with status, totals, and breach details
    """
    if not procs:
        logger.warning(f"Process not found: {name}")
        return {
            "name":         name,
            "status":       "not_found",
            "instances":    [],
            "total_cpu":    0,
            "total_mem_mb": 0,
            "breaches":     ["Process not running"],
        }

    logger.info(
        f"Sampling {len(procs)} instance(s) of '{name}' "
        f"({samples} samples @ {interval:.1f}s interval)..."
    )

    stats = [s for proc in procs if (s := sample_process(proc, samples, interval))]

    if not stats:
        return {
            "name":         name,
            "status":       "error",
            "instances":    [],
            "total_cpu":    0,
            "total_mem_mb": 0,
            "breaches":     ["All instances vanished during sampling"],
        }

    total_cpu = round(sum(s["cpu_percent"] for s in stats), 2)
    total_mem = round(sum(s["mem_mb"]      for s in stats), 2)

    for s in stats:
        logger.info(
            "  PID %-7d  CPU: %5.1f%%  MEM: %7.1f MB  [%s]",
            s["pid"], s["cpu_percent"], s["mem_mb"], s["status"]
        )

    breaches = []
    if total_cpu > cpu_threshold:
        breaches.append(f"Total CPU {total_cpu}% exceeds threshold {cpu_threshold}%")
    if total_mem > mem_threshold_mb:
        breaches.append(f"Total MEM {total_mem:.1f} MB exceeds threshold {mem_threshold_mb:.1f} MB")

    status = "critical" if breaches else "ok"
    logger.info(f"  => Total CPU: {total_cpu:.1f}%  Total MEM: {total_mem:.1f} MB  [{status.upper()}]")

    return {
        "name":         name,
        "status":       status,
        "instances":    stats,
        "total_cpu":    total_cpu,
        "total_mem_mb": total_mem,
        "breaches":     breaches,
    }


def build_alert_message(results, cpu_threshold, mem_threshold_mb):
    """Build a formatted alert message from process check results."""
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


# ── entrypoint ────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Monitor CPU/memory of specific processes")
    parser.add_argument("--processes", nargs="+", help="Process names to monitor")
    parser.add_argument("--pid",       type=int,   help="Monitor a specific PID")
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

    config       = load_config()
    proc_config  = config.get("process_monitor", {})
    cpu_threshold = args.cpu_threshold or proc_config.get("cpu_threshold",    DEFAULT_CPU_THRESHOLD)
    mem_threshold = args.mem_threshold or proc_config.get("mem_threshold_mb", DEFAULT_MEM_THRESHOLD_MB)

    results = []

    if args.pid:
        try:
            proc   = psutil.Process(args.pid)
            name   = proc.name()
            result = check_process_group(
                name, [proc], cpu_threshold, mem_threshold, args.samples, args.interval
            )
            results.append(result)
        except psutil.NoSuchProcess:
            logger.error(f"No process with PID {args.pid}")
            sys.exit(1)
    else:
        process_names = args.processes or proc_config.get("processes", [])
        if not process_names:
            logger.error(
                "No processes specified. Use --processes or set "
                "process_monitor.processes in config.json"
            )
            sys.exit(1)

        for name in process_names:
            procs  = find_processes(name)
            result = check_process_group(
                name, procs, cpu_threshold, mem_threshold, args.samples, args.interval
            )
            results.append(result)

    issues = [r for r in results if r["status"] != "ok"]
    logger.info(f"Summary — {len(results)} process group(s) checked, {len(issues)} with issues")

    if issues and not args.no_alert:
        message = build_alert_message(issues, cpu_threshold, mem_threshold)
        from alert import send_alert
        send_alert(config, "[PROCESS ALERT] CPU/Memory threshold exceeded", message)

    sys.exit(1 if issues else 0)


if __name__ == "__main__":
    main()