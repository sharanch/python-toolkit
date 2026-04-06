"""
tests/test_new_scripts.py

Unit tests for:
  - cert_expiry_checker.py
  - process_monitor.py
  - k8s_pod_restarts.py
  - runbook_runner.py
"""

import json
import ssl
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ===========================================================================
# cert_expiry_checker
# ===========================================================================

class TestCertExpiryChecker:
    def _make_expiry(self, days_from_now: int) -> datetime:
        return datetime.now(tz=timezone.utc) + timedelta(days=days_from_now)

    def test_ok_status_when_far_future(self):
        from scripts.cert_expiry_checker import check_domain
        expiry = self._make_expiry(90)
        with patch("scripts.cert_expiry_checker.get_cert_expiry", return_value=expiry):
            result = check_domain("example.com", 443, warn_days=30, critical_days=7)
        assert result["status"] == "ok"
        assert result["days_remaining"] >= 89

    def test_warning_status_within_warn_window(self):
        from scripts.cert_expiry_checker import check_domain
        expiry = self._make_expiry(20)
        with patch("scripts.cert_expiry_checker.get_cert_expiry", return_value=expiry):
            result = check_domain("example.com", 443, warn_days=30, critical_days=7)
        assert result["status"] == "warning"

    def test_critical_status_within_critical_window(self):
        from scripts.cert_expiry_checker import check_domain
        expiry = self._make_expiry(3)
        with patch("scripts.cert_expiry_checker.get_cert_expiry", return_value=expiry):
            result = check_domain("example.com", 443, warn_days=30, critical_days=7)
        assert result["status"] == "critical"

    def test_ssl_verification_error_is_critical(self):
        from scripts.cert_expiry_checker import check_domain
        with patch(
            "scripts.cert_expiry_checker.get_cert_expiry",
            side_effect=ssl.SSLCertVerificationError("cert verify failed"),
        ):
            result = check_domain("bad.example.com", 443, warn_days=30, critical_days=7)
        assert result["status"] == "critical"
        assert result["error"] is not None

    def test_connection_error_is_error_status(self):
        from scripts.cert_expiry_checker import check_domain
        with patch(
            "scripts.cert_expiry_checker.get_cert_expiry",
            side_effect=ConnectionRefusedError("refused"),
        ):
            result = check_domain("unreachable.example.com", 443, warn_days=30, critical_days=7)
        assert result["status"] == "error"

    def test_alert_message_contains_domain(self):
        from scripts.cert_expiry_checker import build_alert_message
        results = [
            {"hostname": "expiring.example.com", "port": 443, "status": "critical",
             "days_remaining": 3, "expiry": "2026-04-09 00:00:00 UTC", "error": None},
            {"hostname": "ok.example.com", "port": 443, "status": "ok",
             "days_remaining": 90, "expiry": "2026-07-05 00:00:00 UTC", "error": None},
        ]
        msg = build_alert_message(results, warn_days=30, critical_days=7)
        assert "expiring.example.com" in msg
        assert "CRITICAL" in msg.upper()

    def test_alert_message_no_ok_domains(self):
        from scripts.cert_expiry_checker import build_alert_message
        results = [
            {"hostname": "ok.example.com", "port": 443, "status": "ok",
             "days_remaining": 90, "expiry": "2026-07-05 00:00:00 UTC", "error": None},
        ]
        msg = build_alert_message(results, warn_days=30, critical_days=7)
        # OK domains should not appear in the alert
        assert "CRITICAL" not in msg.upper()
        assert "WARNING" not in msg.upper()


# ===========================================================================
# process_monitor
# ===========================================================================

class TestProcessMonitor:
    def _mock_process(self, pid=1234, name="nginx", cpu=10.0, rss_mb=100.0, status="running"):
        proc = MagicMock()
        proc.pid = pid
        proc.name.return_value = name
        proc.status.return_value = status
        proc.cpu_percent.return_value = cpu
        mem_info = MagicMock()
        mem_info.rss = int(rss_mb * 1024 * 1024)
        proc.memory_info.return_value = mem_info
        return proc

    def test_ok_when_under_thresholds(self):
        from scripts.process_monitor import check_process_group
        proc = self._mock_process(cpu=10.0, rss_mb=100.0)
        with patch("time.sleep"):
            result = check_process_group("nginx", [proc], cpu_threshold=80.0,
                                         mem_threshold_mb=512.0, samples=2, interval=0.1)
        assert result["status"] == "ok"
        assert result["breaches"] == []

    def test_critical_when_cpu_exceeded(self):
        from scripts.process_monitor import check_process_group
        proc = self._mock_process(cpu=95.0, rss_mb=100.0)
        with patch("time.sleep"):
            result = check_process_group("nginx", [proc], cpu_threshold=80.0,
                                         mem_threshold_mb=512.0, samples=2, interval=0.1)
        assert result["status"] == "critical"
        assert any("CPU" in b for b in result["breaches"])

    def test_critical_when_memory_exceeded(self):
        from scripts.process_monitor import check_process_group
        proc = self._mock_process(cpu=10.0, rss_mb=1024.0)
        with patch("time.sleep"):
            result = check_process_group("nginx", [proc], cpu_threshold=80.0,
                                         mem_threshold_mb=512.0, samples=2, interval=0.1)
        assert result["status"] == "critical"
        assert any("MEM" in b for b in result["breaches"])

    def test_not_found_when_no_processes(self):
        from scripts.process_monitor import check_process_group
        result = check_process_group("nonexistent", [], cpu_threshold=80.0,
                                     mem_threshold_mb=512.0, samples=1, interval=0.1)
        assert result["status"] == "not_found"

    def test_multiple_instances_aggregate_cpu(self):
        from scripts.process_monitor import check_process_group
        procs = [
            self._mock_process(pid=1, cpu=30.0, rss_mb=100.0),
            self._mock_process(pid=2, cpu=60.0, rss_mb=100.0),
        ]
        with patch("time.sleep"):
            result = check_process_group("nginx", procs, cpu_threshold=80.0,
                                         mem_threshold_mb=512.0, samples=2, interval=0.1)
        # 30 + 60 = 90 > 80 threshold
        assert result["status"] == "critical"
        assert result["total_cpu"] == pytest.approx(90.0, abs=1.0)

    def test_alert_message_contains_process_name(self):
        from scripts.process_monitor import build_alert_message
        results = [{
            "name": "postgres",
            "status": "critical",
            "instances": [{"pid": 999, "cpu_percent": 90.0, "mem_mb": 600.0}],
            "total_cpu": 90.0,
            "total_mem_mb": 600.0,
            "breaches": ["Total CPU 90.0% exceeds threshold 80.0%"],
        }]
        msg = build_alert_message(results, cpu_threshold=80.0, mem_threshold_mb=512.0)
        assert "postgres" in msg
        assert "90" in msg


# ===========================================================================
# k8s_pod_restarts
# ===========================================================================

class TestK8sPodRestarts:
    def _make_pod(self, name, namespace, restarts, ready=False, phase="Running",
                   last_reason="OOMKilled", exit_code=137):
        pod = MagicMock()
        pod.metadata.name = name
        pod.metadata.namespace = namespace
        pod.status.phase = phase

        cs = MagicMock()
        cs.name = "main"
        cs.restart_count = restarts
        cs.ready = ready

        terminated = MagicMock()
        terminated.reason = last_reason
        terminated.exit_code = exit_code
        cs.last_state.terminated = terminated

        pod.status.container_statuses = [cs]
        return pod

    def test_detects_crashlooping_pod(self):
        from scripts.k8s_pod_restarts import check_namespace
        v1 = MagicMock()
        pod = self._make_pod("crasher", "default", restarts=10)
        v1.list_namespaced_pod.return_value.items = [pod]

        issues = check_namespace(v1, "default", threshold=5)
        assert len(issues) == 1
        assert issues[0]["pod"] == "crasher"
        assert issues[0]["restarts"] == 10

    def test_ignores_pod_below_threshold(self):
        from scripts.k8s_pod_restarts import check_namespace
        v1 = MagicMock()
        pod = self._make_pod("stable-pod", "default", restarts=2)
        v1.list_namespaced_pod.return_value.items = [pod]

        issues = check_namespace(v1, "default", threshold=5)
        assert issues == []

    def test_pod_at_threshold_is_flagged(self):
        from scripts.k8s_pod_restarts import check_namespace
        v1 = MagicMock()
        pod = self._make_pod("borderline", "default", restarts=5)
        v1.list_namespaced_pod.return_value.items = [pod]

        issues = check_namespace(v1, "default", threshold=5)
        assert len(issues) == 1

    def test_pod_without_container_statuses_skipped(self):
        from scripts.k8s_pod_restarts import check_namespace
        v1 = MagicMock()
        pod = MagicMock()
        pod.metadata.name = "no-status"
        pod.metadata.namespace = "default"
        pod.status.phase = "Pending"
        pod.status.container_statuses = None
        v1.list_namespaced_pod.return_value.items = [pod]

        issues = check_namespace(v1, "default", threshold=5)
        assert issues == []

    def test_alert_message_groups_by_namespace(self):
        from scripts.k8s_pod_restarts import build_alert_message
        issues = [
            {"namespace": "prod", "pod": "api-abc", "container": "main",
             "restarts": 15, "ready": False, "phase": "Running", "last_state": "OOMKilled (exit 137)"},
            {"namespace": "staging", "pod": "worker-xyz", "container": "worker",
             "restarts": 8, "ready": False, "phase": "Running", "last_state": "Error (exit 1)"},
        ]
        msg = build_alert_message(issues, threshold=5)
        assert "prod" in msg
        assert "staging" in msg
        assert "api-abc" in msg
        assert "15" in msg


# ===========================================================================
# runbook_runner
# ===========================================================================

class TestRunbookRunner:
    def test_render_template_substitutes_variables(self):
        from scripts.runbook_runner import render_template
        result = render_template("Deploy {{ service }} to {{ env }}", {"service": "api", "env": "prod"})
        assert result == "Deploy api to prod"

    def test_render_template_leaves_unknown_vars(self):
        from scripts.runbook_runner import render_template
        result = render_template("Hello {{ unknown }}", {})
        assert "{{ unknown }}" in result

    def test_sleep_step_dry_run(self):
        from scripts.runbook_runner import run_sleep_step
        ok, detail = run_sleep_step({"seconds": 10}, dry_run=True)
        assert ok is True
        assert "dry-run" in detail

    def test_sleep_step_actually_sleeps(self):
        from scripts.runbook_runner import run_sleep_step
        with patch("time.sleep") as mock_sleep:
            ok, detail = run_sleep_step({"seconds": 5}, dry_run=False)
        mock_sleep.assert_called_once_with(5)
        assert ok is True

    def test_shell_step_success(self):
        from scripts.runbook_runner import run_shell_step
        ok, detail = run_shell_step({"command": "echo hello", "timeout": 5, "retries": 0}, dry_run=False)
        assert ok is True

    def test_shell_step_failure(self):
        from scripts.runbook_runner import run_shell_step
        ok, detail = run_shell_step({"command": "false", "timeout": 5, "retries": 0}, dry_run=False)
        assert ok is False

    def test_shell_step_dry_run(self):
        from scripts.runbook_runner import run_shell_step
        ok, detail = run_shell_step({"command": "rm -rf /", "timeout": 5}, dry_run=True)
        assert ok is True
        assert "dry-run" in detail

    def test_http_step_success(self):
        from scripts.runbook_runner import run_http_step
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.elapsed.total_seconds.return_value = 0.05
        with patch("requests.request", return_value=mock_resp):
            ok, detail = run_http_step(
                {"url": "http://example.com/health", "expected_status": 200, "timeout": 5, "retries": 0},
                dry_run=False
            )
        assert ok is True

    def test_http_step_wrong_status(self):
        from scripts.runbook_runner import run_http_step
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.elapsed.total_seconds.return_value = 0.1
        with patch("requests.request", return_value=mock_resp):
            ok, detail = run_http_step(
                {"url": "http://example.com/health", "expected_status": 200, "timeout": 5, "retries": 0},
                dry_run=False
            )
        assert ok is False

    def test_runbook_aborts_on_step_failure(self):
        from scripts.runbook_runner import run_runbook
        runbook = {
            "name": "Test",
            "on_failure": "abort",
            "steps": [
                {"name": "Fail step", "type": "shell", "command": "false", "timeout": 5, "retries": 0},
                {"name": "Should skip", "type": "sleep", "seconds": 1},
            ]
        }
        with patch("time.sleep"):
            summary = run_runbook(runbook, config={}, variables={}, dry_run=False)

        assert summary["success"] is False
        assert summary["aborted"] is True
        skipped = [s for s in summary["steps"] if s["status"] == "skipped"]
        assert len(skipped) == 1

    def test_runbook_continues_on_step_failure(self):
        from scripts.runbook_runner import run_runbook
        runbook = {
            "name": "Test",
            "on_failure": "continue",
            "steps": [
                {"name": "Fail step", "type": "shell", "command": "false", "timeout": 5, "retries": 0},
                {"name": "Still runs", "type": "shell", "command": "echo ok", "timeout": 5, "retries": 0},
            ]
        }
        summary = run_runbook(runbook, config={}, variables={}, dry_run=False)
        assert summary["aborted"] is False
        statuses = [s["status"] for s in summary["steps"]]
        assert "skipped" not in statuses

    def test_runbook_dry_run_no_side_effects(self):
        from scripts.runbook_runner import run_runbook
        runbook = {
            "name": "Dry run test",
            "on_failure": "abort",
            "steps": [
                {"name": "Shell step", "type": "shell", "command": "rm -rf /important", "timeout": 5},
                {"name": "Sleep", "type": "sleep", "seconds": 60},
            ]
        }
        with patch("subprocess.run") as mock_run, patch("time.sleep") as mock_sleep:
            summary = run_runbook(runbook, config={}, variables={}, dry_run=True)

        mock_run.assert_not_called()
        mock_sleep.assert_not_called()
        assert summary["success"] is True

    def test_runbook_variable_substitution(self):
        from scripts.runbook_runner import run_runbook
        runbook = {
            "name": "Var test",
            "on_failure": "abort",
            "steps": [
                {"name": "Echo env", "type": "shell", "command": "echo {{ env }}", "timeout": 5, "retries": 0},
            ]
        }
        with patch("subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = "prod\n"
            mock_proc.stderr = ""
            mock_run.return_value = mock_proc
            summary = run_runbook(runbook, config={}, variables={"env": "prod"}, dry_run=False)

        called_args = mock_run.call_args[0][0]
        assert "prod" in called_args

    def test_runbook_from_yaml_file(self):
        from scripts.runbook_runner import run_runbook
        import yaml
        runbook_yaml = textwrap.dedent("""
            name: YAML file test
            on_failure: abort
            steps:
              - name: Check date
                type: shell
                command: date
                timeout: 5
        """)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(runbook_yaml)
            tmp = Path(f.name)

        try:
            with open(tmp) as f:
                runbook = yaml.safe_load(f)
            summary = run_runbook(runbook, config={}, variables={}, dry_run=False)
            assert summary["success"] is True
        finally:
            tmp.unlink()
