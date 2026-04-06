#!/usr/bin/env python3
"""
runbook_runner.py

Execute YAML-defined runbooks: ordered steps that run shell commands,
HTTP checks, or send Slack notifications. Each step can have retries,
timeouts, and on-failure behaviour (abort or continue).

Designed for incident response automation, maintenance windows, and
repeatable operational procedures.

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

      - name: Check process
        type: shell
        command: "pgrep -x nginx"
        on_failure: continue   # override global policy per-step
"""

import argparse
import json
import logging
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

import requests

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required — run: pip install pyyaml")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.alert import send_slack_alert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.json"

# Step result constants
RESULT_OK = "ok"
RESULT_FAILED = "failed"
RESULT_SKIPPED = "skipped"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def render_template(text: str, variables: dict) -> str:
    """
    Replace {{ var }} placeholders with values from variables dict.
    Unknown variables are left as-is.
    """
    def replacer(match):
        key = match.group(1).strip()
        return str(variables.get(key, match.group(0)))
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", replacer, str(text))


def render_step(step: dict, variables: dict) -> dict:
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


# ---------------------------------------------------------------------------
# Step executors
# ---------------------------------------------------------------------------

def run_shell_step(step: dict, dry_run: bool) -> tuple[bool, str]:
    command = step["command"]
    timeout = step.get("timeout", 60)
    retries = step.get("retries", 0)
    shell = step.get("shell", False)  # True to run via bash -c

    if dry_run:
        log.info("  [DRY RUN] Would run: %s", command)
        return True, "dry-run"

    attempt = 0
    while attempt <= retries:
        try:
            if attempt > 0:
                log.info("  Retry %d/%d...", attempt, retries)

            args = command if shell else shlex.split(command)
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=shell,
            )

            stdout = result.stdout.strip()
            stderr = result.stderr.strip()

            if stdout:
                for line in stdout.splitlines():
                    log.info("  stdout: %s", line)
            if stderr:
                for line in stderr.splitlines():
                    log.warning("  stderr: %s", line)

            if result.returncode == 0:
                return True, stdout or "exit 0"
            else:
                output = f"exit {result.returncode}: {stderr or stdout}"
                log.warning("  Command failed: %s", output)
                attempt += 1

        except subprocess.TimeoutExpired:
            log.error("  Command timed out after %ds", timeout)
            attempt += 1
        except FileNotFoundError as e:
            return False, f"Command not found: {e}"

    return False, f"Failed after {retries + 1} attempt(s)"


def run_http_step(step: dict, dry_run: bool) -> tuple[bool, str]:
    url = step["url"]
    method = step.get("method", "GET").upper()
    expected_status = step.get("expected_status", 200)
    timeout = step.get("timeout", 10)
    retries = step.get("retries", 0)
    headers = step.get("headers", {})
    body = step.get("body")

    if dry_run:
        log.info("  [DRY RUN] Would %s %s (expect %d)", method, url, expected_status)
        return True, "dry-run"

    attempt = 0
    while attempt <= retries:
        try:
            if attempt > 0:
                log.info("  Retry %d/%d...", attempt, retries)

            resp = requests.request(
                method, url,
                headers=headers,
                json=body if body else None,
                timeout=timeout,
            )

            log.info("  %s %s -> %d (%dms)", method, url, resp.status_code,
                     int(resp.elapsed.total_seconds() * 1000))

            if resp.status_code == expected_status:
                return True, f"HTTP {resp.status_code}"

            log.warning("  Expected %d, got %d", expected_status, resp.status_code)
            attempt += 1

        except requests.exceptions.Timeout:
            log.warning("  Request timed out after %ds", timeout)
            attempt += 1
        except requests.exceptions.ConnectionError as e:
            log.warning("  Connection error: %s", e)
            attempt += 1

    return False, f"HTTP check failed after {retries + 1} attempt(s)"


def run_sleep_step(step: dict, dry_run: bool) -> tuple[bool, str]:
    seconds = step.get("seconds", 1)
    if dry_run:
        log.info("  [DRY RUN] Would sleep %ds", seconds)
        return True, "dry-run"
    log.info("  Sleeping %ds...", seconds)
    time.sleep(seconds)
    return True, f"slept {seconds}s"


def run_slack_step(step: dict, config: dict, dry_run: bool) -> tuple[bool, str]:
    message = step.get("message", "")
    webhook = step.get("webhook_url") or config.get("alerts", {}).get("slack_webhook_url")

    if dry_run:
        log.info("  [DRY RUN] Would send Slack: %s", message)
        return True, "dry-run"

    if not webhook:
        log.warning("  No Slack webhook configured — skipping")
        return True, "skipped (no webhook)"

    send_slack_alert(message, webhook)
    return True, "sent"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def execute_step(step: dict, config: dict, variables: dict, dry_run: bool) -> dict:
    step = render_step(step, variables)
    step_name = step.get("name", "(unnamed)")
    step_type = step.get("type", "shell")

    log.info("--- Step: %s [%s]", step_name, step_type)

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
            log.error("  Unknown step type: %s", step_type)
            ok, detail = False, f"unknown type: {step_type}"
    except Exception as e:
        log.exception("  Unexpected error in step: %s", e)
        ok, detail = False, str(e)

    elapsed = round(time.monotonic() - start, 2)
    status = RESULT_OK if ok else RESULT_FAILED
    log.info("  [%s] in %.2fs — %s", status.upper(), elapsed, detail)

    return {
        "name": step_name,
        "type": step_type,
        "status": status,
        "detail": detail,
        "elapsed_s": elapsed,
    }


def run_runbook(runbook: dict, config: dict, variables: dict, dry_run: bool) -> dict:
    name = runbook.get("name", "Unnamed Runbook")
    description = runbook.get("description", "")
    steps = runbook.get("steps", [])
    global_on_failure = runbook.get("on_failure", "abort")

    log.info("=" * 60)
    log.info("Runbook: %s", name)
    if description:
        log.info("         %s", description)
    if dry_run:
        log.info("         *** DRY RUN MODE — no changes will be made ***")
    log.info("         Steps: %d  |  on_failure: %s", len(steps), global_on_failure)
    log.info("=" * 60)

    results = []
    aborted = False
    abort_reason = None

    for i, step in enumerate(steps, 1):
        if aborted:
            log.warning("Skipping step %d/%d: %s (runbook aborted)", i, len(steps), step.get("name", ""))
            results.append({
                "name": step.get("name", "(unnamed)"),
                "type": step.get("type", "shell"),
                "status": RESULT_SKIPPED,
                "detail": "runbook aborted",
                "elapsed_s": 0,
            })
            continue

        result = execute_step(step, config, variables, dry_run)
        results.append(result)

        if result["status"] == RESULT_FAILED:
            step_on_failure = step.get("on_failure", global_on_failure)
            if step_on_failure == "abort":
                aborted = True
                abort_reason = f"Step '{result['name']}' failed: {result['detail']}"
                log.error("Runbook aborted: %s", abort_reason)

    ok_count = sum(1 for r in results if r["status"] == RESULT_OK)
    fail_count = sum(1 for r in results if r["status"] == RESULT_FAILED)
    skip_count = sum(1 for r in results if r["status"] == RESULT_SKIPPED)
    total_time = round(sum(r["elapsed_s"] for r in results), 2)

    success = fail_count == 0 and not aborted

    log.info("=" * 60)
    log.info(
        "Result: %s | OK: %d  FAILED: %d  SKIPPED: %d | %.2fs",
        "SUCCESS" if success else "FAILED",
        ok_count, fail_count, skip_count, total_time,
    )
    log.info("=" * 60)

    return {
        "runbook": name,
        "success": success,
        "aborted": aborted,
        "abort_reason": abort_reason,
        "steps": results,
        "ok": ok_count,
        "failed": fail_count,
        "skipped": skip_count,
        "total_time_s": total_time,
    }


def main():
    parser = argparse.ArgumentParser(description="Execute YAML-defined SRE runbooks")
    parser.add_argument("--runbook", required=True, help="Path to runbook YAML file")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate execution without making any changes",
    )
    parser.add_argument(
        "--var",
        action="append",
        metavar="KEY=VALUE",
        help="Template variable (repeatable): --var env=prod --var region=us-east-1",
    )
    parser.add_argument(
        "--output",
        help="Write JSON results to this file",
    )
    args = parser.parse_args()

    # Parse --var key=value pairs
    variables: dict[str, str] = {}
    for pair in (args.var or []):
        if "=" not in pair:
            log.error("Invalid --var format (expected KEY=VALUE): %s", pair)
            sys.exit(1)
        k, v = pair.split("=", 1)
        variables[k] = v

    runbook_path = Path(args.runbook)
    if not runbook_path.exists():
        log.error("Runbook not found: %s", runbook_path)
        sys.exit(1)

    with open(runbook_path) as f:
        runbook = yaml.safe_load(f)

    config = load_config()
    summary = run_runbook(runbook, config, variables, dry_run=args.dry_run)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(summary, f, indent=2)
        log.info("Results written to %s", output_path)

    sys.exit(0 if summary["success"] else 1)


if __name__ == "__main__":
    main()
