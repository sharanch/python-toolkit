"""
log_parser.py — SRE Log Parser
Parses log files and reports error counts and summaries.

Usage:
    python log_parser.py <log_file>
    python log_parser.py /var/log/app/server.log
"""

import sys
import json
import logging
import os

# ── logging setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# ── config ───────────────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "../config/config.json")


def load_config():
    """Load configuration from config.json."""
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning("config.json not found, using defaults")
        return {"log_parser": {"log_levels": ["INFO", "WARN", "WARNING", "ERROR", "CRITICAL"]}}
    except json.JSONDecodeError as e:
        logger.error(f"Invalid config.json: {e}")
        sys.exit(1)


# ── core logic ────────────────────────────────────────────────────────────────
def parse_log_line(line):
    """
    Parse a single log line into a structured dict.

    Expected format: YYYY-MM-DD HH:MM:SS LEVEL message

    Args:
        line (str): Raw log line

    Returns:
        dict or None: Parsed log entry, or None if line is malformed
    """
    parts = line.strip().split()
    if len(parts) < 4:
        return None

    return {
        "date":    parts[0],
        "time":    parts[1],
        "level":   parts[2],
        "message": " ".join(parts[3:])
    }


def parse_log_file(file_path):
    """
    Read and parse an entire log file.

    Args:
        file_path (str): Path to log file

    Returns:
        list: List of parsed log entry dicts
    """
    entries = []

    if not os.path.exists(file_path):
        logger.error(f"Log file not found: {file_path}")
        sys.exit(1)

    try:
        with open(file_path, "r") as f:
            for line_num, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                entry = parse_log_line(line)
                if entry:
                    entries.append(entry)
                else:
                    logger.warning(f"Skipping malformed line {line_num}: {line}")
    except PermissionError:
        logger.error(f"Permission denied reading: {file_path}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to read log file: {e}")
        sys.exit(1)

    return entries


def summarize(entries):
    """
    Count occurrences of each log level.

    Args:
        entries (list): List of parsed log entry dicts

    Returns:
        dict: Count per log level
    """
    counts = {}
    for entry in entries:
        level = entry["level"]
        counts[level] = counts.get(level, 0) + 1
    return counts


def print_report(file_path, entries, counts):
    """Print a formatted report to stdout."""
    errors = [e for e in entries if e["level"] in ("ERROR", "CRITICAL")]

    print(f"\n=== Log Report: {file_path} ===")
    print(f"Total lines parsed: {len(entries)}\n")

    if errors:
        print("=== ERRORS & CRITICALS ===")
        for e in errors:
            print(f"  {e['date']} {e['time']} [{e['level']}] {e['message']}")
    else:
        print("No errors found!")

    print("\n=== SUMMARY ===")
    for level, count in sorted(counts.items()):
        print(f"  {level}: {count}")
    print()


# ── entrypoint ────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print("ERROR: Please provide a log file path")
        print("Usage: python log_parser.py <log_file>")
        sys.exit(1)

    file_path = sys.argv[1]
    config    = load_config()

    logger.info(f"Parsing log file: {file_path}")
    entries = parse_log_file(file_path)
    counts  = summarize(entries)

    print_report(file_path, entries, counts)

    # exit with error code if any errors found — useful for cron alerting
    error_count = counts.get("ERROR", 0) + counts.get("CRITICAL", 0)
    if error_count > 0:
        logger.warning(f"{error_count} error(s) found in log file")
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
