#!/usr/bin/env python3
"""
k8s_pod_restarts.py — SRE Kubernetes Pod Restart Monitor
Detect crashlooping or frequently restarting pods in a Kubernetes cluster.
Alerts via Slack/email with pod name, namespace, restart count, and last state.

Requires: pip install kubernetes

Usage:
    python scripts/k8s_pod_restarts.py
    python scripts/k8s_pod_restarts.py --namespace production --threshold 5
    python scripts/k8s_pod_restarts.py --all-namespaces --threshold 3
"""

import argparse
import json
import logging
import os
import sys

try:
    from kubernetes import client, config as k8s_config
    from kubernetes.client.exceptions import ApiException
except ImportError:
    print("ERROR: kubernetes client is required — run: pip install kubernetes")
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

DEFAULT_RESTART_THRESHOLD = 5
DEFAULT_NAMESPACE         = "default"


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


# ── kubeconfig ────────────────────────────────────────────────────────────────
def load_kubeconfig(kubeconfig=None, in_cluster=False):
    """
    Load kubeconfig from file, in-cluster service account, or default location.

    Args:
        kubeconfig (str): Optional path to kubeconfig file
        in_cluster (bool): Use in-cluster service account if True
    """
    if in_cluster:
        logger.info("Loading in-cluster kubeconfig")
        k8s_config.load_incluster_config()
        return

    if kubeconfig:
        logger.info(f"Loading kubeconfig from: {kubeconfig}")
        k8s_config.load_kube_config(config_file=kubeconfig)
        return

    try:
        k8s_config.load_incluster_config()
        logger.info("Loaded in-cluster kubeconfig")
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()
        logger.info("Loaded kubeconfig from default location")


# ── core logic ────────────────────────────────────────────────────────────────
def get_last_state_reason(container_status):
    """
    Extract termination reason from last container state if available.

    Args:
        container_status: Kubernetes container status object

    Returns:
        str: Reason string e.g. 'OOMKilled (exit 137)' or 'unknown'
    """
    last = container_status.last_state
    if last and last.terminated:
        return f"{last.terminated.reason} (exit {last.terminated.exit_code})"
    return "unknown"


def check_namespace(v1, namespace, threshold):
    """
    Check all pods in a namespace for restart count exceeding threshold.

    Args:
        v1: Kubernetes CoreV1Api client
        namespace (str): Namespace to check
        threshold (int): Restart count threshold

    Returns:
        list: Dicts describing each container exceeding the threshold
    """
    issues = []

    try:
        pods = v1.list_namespaced_pod(namespace=namespace)
    except ApiException as e:
        logger.error(f"Failed to list pods in namespace '{namespace}': {e}")
        return issues

    for pod in pods.items:
        pod_name      = pod.metadata.name
        pod_namespace = pod.metadata.namespace
        phase         = pod.status.phase or "Unknown"

        if not pod.status.container_statuses:
            continue

        for cs in pod.status.container_statuses:
            restarts = cs.restart_count

            if restarts >= threshold:
                last_reason = get_last_state_reason(cs)
                issues.append({
                    "namespace":  pod_namespace,
                    "pod":        pod_name,
                    "container":  cs.name,
                    "restarts":   restarts,
                    "ready":      cs.ready,
                    "phase":      phase,
                    "last_state": last_reason,
                })
                logger.warning(
                    "%-50s  container: %-20s  restarts: %3d  ready: %s  last: %s",
                    f"{pod_namespace}/{pod_name}",
                    cs.name,
                    restarts,
                    cs.ready,
                    last_reason,
                )

    return issues


def build_alert_message(issues, threshold):
    """Build a formatted alert message from pod restart issues."""
    lines = [
        f"*Kubernetes Pod Restart Alert*",
        f"Pods with restart count ≥ {threshold}\n",
    ]

    # group by namespace
    by_ns = {}
    for issue in issues:
        by_ns.setdefault(issue["namespace"], []).append(issue)

    for ns, ns_issues in sorted(by_ns.items()):
        lines.append(f"*Namespace: `{ns}`*")
        for i in ns_issues:
            ready_sym = ":white_check_mark:" if i["ready"] else ":x:"
            lines.append(f"  • `{i['pod']}` / `{i['container']}`")
            lines.append(
                f"    Restarts: *{i['restarts']}*  |  Ready: {ready_sym}  "
                f"|  Phase: {i['phase']}  |  Last exit: `{i['last_state']}`"
            )
        lines.append("")

    lines.append(f"Total affected containers: {len(issues)}")
    return "\n".join(lines)


# ── entrypoint ────────────────────────────────────────────────────────────────
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

    config     = load_config()
    k8s_cfg    = config.get("k8s_monitor", {})
    threshold  = args.threshold or k8s_cfg.get("restart_threshold", DEFAULT_RESTART_THRESHOLD)

    try:
        load_kubeconfig(kubeconfig=args.kubeconfig, in_cluster=args.in_cluster)
    except Exception as e:
        logger.error(f"Failed to load kubeconfig: {e}")
        sys.exit(1)

    v1         = client.CoreV1Api()
    all_issues = []

    if args.all_namespaces:
        try:
            ns_list    = v1.list_namespace()
            namespaces = [ns.metadata.name for ns in ns_list.items]
            logger.info(f"Checking {len(namespaces)} namespace(s): {', '.join(namespaces)}")
        except ApiException as e:
            logger.error(f"Failed to list namespaces: {e}")
            sys.exit(1)
    else:
        namespaces = k8s_cfg.get("namespaces", [args.namespace])
        logger.info(f"Checking namespace(s): {', '.join(namespaces)}")

    for ns in namespaces:
        issues = check_namespace(v1, ns, threshold)
        all_issues.extend(issues)

    logger.info(
        f"Summary — {len(namespaces)} namespace(s) checked, "
        f"{len(all_issues)} container(s) exceeding restart threshold of {threshold}"
    )

    if all_issues and not args.no_alert:
        message = build_alert_message(all_issues, threshold)
        from alert import send_alert
        send_alert(config, f"[K8S ALERT] {len(all_issues)} container(s) crashlooping", message)

    sys.exit(1 if all_issues else 0)


if __name__ == "__main__":
    main()