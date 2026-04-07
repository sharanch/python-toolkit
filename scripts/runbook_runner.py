#!/usr/bin/env python3
"""
runbook_runner.py — SRE Runbook Runner
Execute YAML-defined runbooks: ordered steps that run shell commands,
HTTP checks, or send Slack notifications. Each step supports retries,
timeouts, and on-failure behaviour (abort or continue).

Designed for incident response automation, maintenance windows, and
repeatable operational procedures.

Requires: pip install pyyaml requests

Usage:
    python scripts/runbook_runner.py --runbook runbooks/restart_service.yaml
    python scripts/runbook_runner.py --runbook runbooks/drain_node.yaml --dry-run
    python scripts/runbook_runner.py --runbook runbooks/healthcheck_suite.yaml --var env=prod

Runbook YAML format:
    name: Restart nginx and verify
    description: Gracefully restart nginx, wait, then confirm health
    on_failure: abort        # abort (default) | continue
    steps:
      - name: Notify team
        type: slack
        message: "Starting nginx restart on {{ hostname }}"

      - name: Reload nginx
        type: shell
        command: "systemctl reload nginx"
        timeout: 30
        retries: 2

      - name: Wait for reload
        type: sleep
        seconds: 5

      - name: Health check
        type: http
        url: "http://localhost/health"
        expected_status: 200
        timeout: 10
        retries: 3
"""

import argparse
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time

try:
    import requests
except ImportError:
    print("ERROR: requests is required — run: pip install requests")
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required — run: pip install pyyaml")
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

RESULT_OK      = "ok"
RESULT_FAILED  = "failed"
RESULT_SKIPPED = "skipped"


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


# ── template rendering ────────────────────────────────────────────────────────
def render_template(text, variables):
    """
    Replace {{ var }} placeholders with values from variables dict.
    Unknown variables are left as-is.

    Args:
        text (str): Template string
        variables (dict): Variable substitutions

    Returns:
        str: Rendered string
    """
    def replacer(match):
        key = match.group(1).strip()
        return str(variables.get(key, match.group(0)))
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", replacer, str(text))


def render_step(step, variables):
    """Recursively render template variables in all string values of a step."""
    rendered = {}
    for k, v in step.items():
        if isinstance(v, str):
            rendered[k] = render_template(v, variables)
        elif isinstance(v, dict):
            rendered[k] = render_step(v, variables)
        else:
            rendered[k] = v
    return rendered


# ── step executors ────────────────────────────────────────────────────────────
def run_shell_step(step, dry_run):
    """
    Run a shell command step.

    Args:
        step (dict): Step config with command, timeout, retries
        dry_run (bool): If True, print what would run without executing

    Returns:
        tuple: (success bool, detail string)
    """
    command = step["command"]
    timeout = step.get("timeout", 60)
    retries = step.get("retries", 0)
    shell   = step.get("shell", False)

    if dry_run:
        logger.info(f"  [DRY RUN] Would run: {command}")
        return True, "dry-run"

    attempt = 0
    while attempt <= retries:
        try:
            if attempt > 0:
                logger.info(f"  Retry {attempt}/{retries}...")

            args   = command if shell else shlex.split(command)
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=shell,
            )

            if result.stdout.strip():
                for line in result.stdout.strip().splitlines():
                    logger.info(f"  stdout: {line}")
            if result.stderr.strip():
                for line in result.stderr.strip().splitlines():
                    logger.warning(f"  stderr: {line}")

            if result.returncode == 0:
                return True, result.stdout.strip() or "exit 0"

            logger.warning(f"  Command failed: exit {result.returncode}")
            attempt += 1

        except subprocess.TimeoutExpired:
            logger.error(f"  Command timed out after {timeout}s")
            attempt += 1
        except FileNotFoundError as e:
            return False, f"Command not found: {e}"

    return False, f"Failed after {retries + 1} attempt(s)"


def run_http_step(step, dry_run):
    """
    Run an HTTP health check step.

    Args:
        step (dict): Step config with url, method, expected_status, timeout, retries
        dry_run (bool): If True, skip execution

    Returns:
        tuple: (success bool, detail string)
    """
    url             = step["url"]
    method          = step.get("method", "GET").upper()
    expected_status = step.get("expected_status", 200)
    timeout         = step.get("timeout", 10)
    retries         = step.get("retries", 0)
    headers         = step.get("headers", {})
    body            = step.get("body")

    if dry_run:
        logger.info(f"  [DRY RUN] Would {method} {url} (expect {expected_status})")
        return True, "dry-run"

    attempt = 0
    while attempt <= retries:
        try:
            if attempt > 0:
                logger.info(f"  Retry {attempt}/{retries}...")

            resp = requests.request(
                method, url,
                headers=headers,
                json=body if body else None,
                timeout=timeout,
            )
            elapsed_ms = int(resp.elapsed.total_seconds() * 1000)
            logger.info(f"  {method} {url} -> {resp.status_code} ({elapsed_ms}ms)")

            if resp.status_code == expected_status:
                return True, f"HTTP {resp.status_code}"

            logger.warning(f"  Expected {expected_status}, got {resp.status_code}")
            attempt += 1

        except requests.exceptions.Timeout:
            logger.warning(f"  Request timed out after {timeout}s")
            attempt += 1
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"  Connection error: {e}")
            attempt += 1

    return False, f"HTTP check failed after {retries + 1} attempt(s)"


def run_sleep_step(step, dry_run):
    """Run a sleep/wait step."""
    seconds = step.get("seconds", 1)
    if dry_run:
        logger.info(f"  [DRY RUN] Would sleep {seconds}s")
        return True, "dry-run"
    logger.info(f"  Sleeping {seconds}s...")
    time.sleep(seconds)
    return True, f"slept {seconds}s"


def run_slack_step(step, config, dry_run):
    """Run a Slack notification step."""
    message = step.get("message", "")
    webhook = step.get("webhook_url") or config.get("alerts", {}).get("slack_webhook_url")

    if dry_run:
        logger.info(f"  [DRY RUN] Would send Slack: {message}")
        return True, "dry-run"

    if not webhook:
        logger.warning("  No Slack webhook configured — skipping")
        return True, "skipped (no webhook)"

    from alert import send_slack
    send_slack(webhook, message)
    return True, "sent"


# ── runner ────────────────────────────────────────────────────────────────────
def execute_step(step, config, variables, dry_run):
    """
    Execute a single runbook step.

    Args:
        step (dict): Step definition
        config (dict): Full project config
        variables (dict): Template variables
        dry_run (bool): Dry run mode

    Returns:
        dict: Step result with status, detail, elapsed_s
    """
    step      = render_step(step, variables)
    step_name = step.get("name", "(unnamed)")
    step_type = step.get("type", "shell")

    logger.info(f"--- Step: {step_name} [{step_type}]")

    start = time.monotonic()
    try:
        if step_type == "shell":
            ok, detail = run_shell_step(step, dry_run)
        elif step_type == "http":
            ok, detail = run_http_step(step, dry_run)
        elif step_type == "sleep":
            ok, detail = run_sleep_step(step, dry_run)
        elif step_type == "slack":
            ok, detail = run_slack_step(step, config, dry_run)
        else:
            logger.error(f"  Unknown step type: {step_type}")
            ok, detail = False, f"unknown type: {step_type}"
    except Exception as e:
        logger.exception(f"  Unexpected error in step: {e}")
        ok, detail = False, str(e)

    elapsed = round(time.monotonic() - start, 2)
    status  = RESULT_OK if ok else RESULT_FAILED
    logger.info(f"  [{status.upper()}] in {elapsed:.2f}s — {detail}")

    return {
        "name":      step_name,
        "type":      step_type,
        "status":    status,
        "detail":    detail,
        "elapsed_s": elapsed,
    }


def run_runbook(runbook, config, variables, dry_run):
    """
    Execute all steps in a runbook.

    Args:
        runbook (dict): Parsed runbook YAML
        config (dict): Full project config
        variables (dict): Template variables from CLI
        dry_run (bool): Dry run mode

    Returns:
        dict: Summary with success flag, step results, counts, and total time
    """
    name             = runbook.get("name", "Unnamed Runbook")
    description      = runbook.get("description", "")
    steps            = runbook.get("steps", [])
    global_on_failure = runbook.get("on_failure", "abort")

    logger.info("=" * 60)
    logger.info(f"Runbook: {name}")
    if description:
        logger.info(f"         {description}")
    if dry_run:
        logger.info("         *** DRY RUN MODE — no changes will be made ***")
    logger.info(f"         Steps: {len(steps)}  |  on_failure: {global_on_failure}")
    logger.info("=" * 60)

    results      = []
    aborted      = False
    abort_reason = None

    for i, step in enumerate(steps, 1):
        if aborted:
            logger.warning(f"Skipping step {i}/{len(steps)}: {step.get('name', '')} (runbook aborted)")
            results.append({
                "name":      step.get("name", "(unnamed)"),
                "type":      step.get("type", "shell"),
                "status":    RESULT_SKIPPED,
                "detail":    "runbook aborted",
                "elapsed_s": 0,
            })
            continue

        result = execute_step(step, config, variables, dry_run)
        results.append(result)

        if result["status"] == RESULT_FAILED:
            step_on_failure = step.get("on_failure", global_on_failure)
            if step_on_failure == "abort":
                aborted      = True
                abort_reason = f"Step '{result['name']}' failed: {result['detail']}"
                logger.error(f"Runbook aborted: {abort_reason}")

    ok_count   = sum(1 for r in results if r["status"] == RESULT_OK)
    fail_count = sum(1 for r in results if r["status"] == RESULT_FAILED)
    skip_count = sum(1 for r in results if r["status"] == RESULT_SKIPPED)
    total_time = round(sum(r["elapsed_s"] for r in results), 2)
    success    = fail_count == 0 and not aborted

    logger.info("=" * 60)
    logger.info(
        f"Result: {'SUCCESS' if success else 'FAILED'} | "
        f"OK: {ok_count}  FAILED: {fail_count}  SKIPPED: {skip_count} | {total_time:.2f}s"
    )
    logger.info("=" * 60)

    return {
        "runbook":      name,
        "success":      success,
        "aborted":      aborted,
        "abort_reason": abort_reason,
        "steps":        results,
        "ok":           ok_count,
        "failed":       fail_count,
        "skipped":      skip_count,
        "total_time_s": total_time,
    }


# ── entrypoint ────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Execute YAML-defined SRE runbooks")
    parser.add_argument("--runbook",  required=True, help="Path to runbook YAML file")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Simulate execution without making any changes")
    parser.add_argument("--var",      action="append", metavar="KEY=VALUE",
                        help="Template variable (repeatable): --var env=prod --var region=us-east-1")
    parser.add_argument("--output",   help="Write JSON results to this file")
    args = parser.parse_args()

    # parse --var key=value pairs
    variables = {}
    for pair in (args.var or []):
        if "=" not in pair:
            logger.error(f"Invalid --var format (expected KEY=VALUE): {pair}")
            sys.exit(1)
        k, v = pair.split("=", 1)
        variables[k] = v

    if not os.path.exists(args.runbook):
        logger.error(f"Runbook not found: {args.runbook}")
        sys.exit(1)

    with open(args.runbook, "r") as f:
        runbook = yaml.safe_load(f)

    config  = load_config()
    summary = run_runbook(runbook, config, variables, dry_run=args.dry_run)

    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(summary, f, indent=2)
        logger.info(f"Results written to {args.output}")

    sys.exit(0 if summary["success"] else 1)


if __name__ == "__main__":
    main()