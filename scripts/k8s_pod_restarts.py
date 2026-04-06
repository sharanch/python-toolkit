#!/usr/bin/env python3
"""
k8s_pod_restarts.py

Detect crashlooping or frequently restarting pods in a Kubernetes cluster.
Alerts via Slack/email with pod name, namespace, restart count, and last state.

Requires: pip install kubernetes

Usage:
    python scripts/k8s_pod_restarts.py
    python scripts/k8s_pod_restarts.py --namespace production --threshold 5
    python scripts/k8s_pod_restarts.py --all-namespaces --threshold 3 --kubeconfig ~/.kube/config
"""

import argparse
import json
import logging
import sys
from pathlib import Path

try:
    from kubernetes import client, config as k8s_config
    from kubernetes.client.exceptions import ApiException
except ImportError:
    print("ERROR: kubernetes client is required — run: pip install kubernetes")
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

DEFAULT_RESTART_THRESHOLD = 5
DEFAULT_NAMESPACE = "default"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def load_kubeconfig(kubeconfig: str | None = None, in_cluster: bool = False):
    """Load kubeconfig from file, in-cluster service account, or default locations."""
    if in_cluster:
        log.info("Loading in-cluster kubeconfig")
        k8s_config.load_incluster_config()
        return

    if kubeconfig:
        log.info("Loading kubeconfig from: %s", kubeconfig)
        k8s_config.load_kube_config(config_file=kubeconfig)
        return

    try:
        k8s_config.load_incluster_config()
        log.info("Loaded in-cluster kubeconfig")
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()
        log.info("Loaded kubeconfig from default location")


def get_container_restart_count(container_status) -> int:
    """Sum restarts from current and previous state."""
    return container_status.restart_count


def get_last_state_reason(container_status) -> str:
    """Extract termination reason from last container state if available."""
    last = container_status.last_state
    if last and last.terminated:
        return f"{last.terminated.reason} (exit {last.terminated.exit_code})"
    return "unknown"


def get_pod_phase_symbol(phase: str) -> str:
    return {
        "Running": ":large_green_circle:",
        "Pending": ":large_yellow_circle:",
        "Failed": ":red_circle:",
        "Succeeded": ":white_check_mark:",
        "Unknown": ":white_circle:",
    }.get(phase, ":white_circle:")


def check_namespace(v1: client.CoreV1Api, namespace: str, threshold: int) -> list[dict]:
    """
    Check all pods in a namespace. Return list of pods exceeding restart threshold.
    """
    issues = []

    try:
        pods = v1.list_namespaced_pod(namespace=namespace)
    except ApiException as e:
        log.error("Failed to list pods in namespace '%s': %s", namespace, e)
        return issues

    for pod in pods.items:
        pod_name = pod.metadata.name
        pod_namespace = pod.metadata.namespace
        phase = pod.status.phase or "Unknown"

        if not pod.status.container_statuses:
            continue

        for cs in pod.status.container_statuses:
            restarts = get_container_restart_count(cs)
            ready = cs.ready

            if restarts >= threshold:
                last_reason = get_last_state_reason(cs)
                issue = {
                    "namespace": pod_namespace,
                    "pod": pod_name,
                    "container": cs.name,
                    "restarts": restarts,
                    "ready": ready,
                    "phase": phase,
                    "last_state": last_reason,
                }
                issues.append(issue)
                log.warning(
                    "%-50s  container: %-20s  restarts: %3d  ready: %s  last: %s",
                    f"{pod_namespace}/{pod_name}",
                    cs.name,
                    restarts,
                    ready,
                    last_reason,
                )
            else:
                log.debug(
                    "%-50s  container: %-20s  restarts: %3d  [OK]",
                    f"{pod_namespace}/{pod_name}", cs.name, restarts
                )

    return issues


def build_alert_message(issues: list[dict], threshold: int) -> str:
    lines = [
        f":rotating_light: *Kubernetes Pod Restart Alert*",
        f"Pods with restart count ≥ {threshold}\n",
    ]

    # Group by namespace
    by_ns: dict[str, list] = {}
    for issue in issues:
        by_ns.setdefault(issue["namespace"], []).append(issue)

    for ns, ns_issues in sorted(by_ns.items()):
        lines.append(f"*Namespace: `{ns}`*")
        for i in ns_issues:
            phase_sym = get_pod_phase_symbol(i["phase"])
            ready_sym = ":white_check_mark:" if i["ready"] else ":x:"
            lines.append(
                f"  {phase_sym} `{i['pod']}` / `{i['container']}`"
            )
            lines.append(
                f"    Restarts: *{i['restarts']}*  |  Ready: {ready_sym}  |  Last exit: `{i['last_state']}`"
            )
        lines.append("")

    lines.append(f"_Total affected containers: {len(issues)}_")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Detect crashlooping Kubernetes pods")
    parser.add_argument(
        "--namespace",
        default=DEFAULT_NAMESPACE,
        help=f"Namespace to check (default: {DEFAULT_NAMESPACE})",
    )
    parser.add_argument(
        "--all-namespaces",
        action="store_true",
        help="Check all namespaces (overrides --namespace)",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_RESTART_THRESHOLD,
        help=f"Restart count to trigger alert (default: {DEFAULT_RESTART_THRESHOLD})",
    )
    parser.add_argument(
        "--kubeconfig",
        help="Path to kubeconfig file (defaults to KUBECONFIG env or ~/.kube/config)",
    )
    parser.add_argument(
        "--in-cluster",
        action="store_true",
        help="Use in-cluster service account (for running inside a pod)",
    )
    parser.add_argument("--no-alert", action="store_true", help="Print results only, no alerts")
    args = parser.parse_args()

    config = load_config()
    k8s_cfg = config.get("k8s_monitor", {})
    alerts_config = config.get("alerts", {})

    threshold = args.threshold or k8s_cfg.get("restart_threshold", DEFAULT_RESTART_THRESHOLD)

    try:
        load_kubeconfig(kubeconfig=args.kubeconfig, in_cluster=args.in_cluster)
    except Exception as e:
        log.error("Failed to load kubeconfig: %s", e)
        sys.exit(1)

    v1 = client.CoreV1Api()
    all_issues = []

    if args.all_namespaces:
        try:
            ns_list = v1.list_namespace()
            namespaces = [ns.metadata.name for ns in ns_list.items]
            log.info("Checking %d namespace(s): %s", len(namespaces), ", ".join(namespaces))
        except ApiException as e:
            log.error("Failed to list namespaces: %s", e)
            sys.exit(1)
    else:
        namespaces = k8s_cfg.get("namespaces", [args.namespace])
        log.info("Checking namespace(s): %s", ", ".join(namespaces))

    for ns in namespaces:
        issues = check_namespace(v1, ns, threshold)
        all_issues.extend(issues)

    log.info(
        "Summary — %d namespace(s) checked, %d container(s) exceeding restart threshold of %d",
        len(namespaces), len(all_issues), threshold
    )

    if all_issues and not args.no_alert:
        message = build_alert_message(all_issues, threshold)
        slack_url = alerts_config.get("slack_webhook_url")
        if slack_url:
            send_slack_alert(message, slack_url)
        smtp_user = alerts_config.get("smtp_user")
        if smtp_user:
            send_email_alert(
                subject=f"[K8S ALERT] {len(all_issues)} container(s) crashlooping",
                body=message,
                config=alerts_config,
            )

    sys.exit(1 if all_issues else 0)


if __name__ == "__main__":
    main()
