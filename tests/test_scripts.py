"""
test_scripts.py — Unit tests for sre-toolkit scripts
Run with: python -m pytest tests/ -v
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../scripts"))

from log_parser import parse_log_line, summarize
from disk_monitor import should_send_alert
from health_check import check_service
from cert_expiry_checker import build_alert_message as cert_build_alert
from process_monitor import find_processes, build_alert_message as proc_build_alert
from runbook_runner import render_template, render_step, RESULT_OK, RESULT_FAILED


# ── log_parser ────────────────────────────────────────────────────────────────
class TestParseLogLine:

    def test_parses_valid_error_line(self):
        line   = "2024-01-15 08:25:03 ERROR Disk usage exceeded 90%"
        result = parse_log_line(line)
        assert result["date"]    == "2024-01-15"
        assert result["time"]    == "08:25:03"
        assert result["level"]   == "ERROR"
        assert result["message"] == "Disk usage exceeded 90%"

    def test_parses_valid_info_line(self):
        line   = "2024-01-15 08:26:45 INFO Request handled successfully"
        result = parse_log_line(line)
        assert result["level"]   == "INFO"
        assert result["message"] == "Request handled successfully"

    def test_parses_multiword_message(self):
        line   = "2024-01-15 08:28:01 WARNING Memory usage at 75% on host web-01"
        result = parse_log_line(line)
        assert result["level"]   == "WARNING"
        assert result["message"] == "Memory usage at 75% on host web-01"

    def test_returns_none_for_malformed_line(self):
        assert parse_log_line("not a valid log line") is None

    def test_returns_none_for_empty_line(self):
        assert parse_log_line("") is None

    def test_returns_none_for_partial_line(self):
        assert parse_log_line("2024-01-15 08:25:03") is None


class TestSummarize:

    def test_counts_correctly(self):
        entries = [
            {"level": "INFO"},
            {"level": "INFO"},
            {"level": "ERROR"},
            {"level": "WARN"},
        ]
        counts = summarize(entries)
        assert counts["INFO"]  == 2
        assert counts["ERROR"] == 1
        assert counts["WARN"]  == 1

    def test_empty_list(self):
        assert summarize([]) == {}

    def test_counts_critical(self):
        entries = [{"level": "CRITICAL"}, {"level": "CRITICAL"}]
        counts  = summarize(entries)
        assert counts["CRITICAL"] == 2

    def test_single_entry(self):
        counts = summarize([{"level": "INFO"}])
        assert counts == {"INFO": 1}


# ── disk_monitor ──────────────────────────────────────────────────────────────
class TestShouldSendAlert:

    def test_sends_alert_first_time(self):
        # use a path that doesn't exist — should send
        state_file = "/tmp/sre_test_no_such_file.json"
        if os.path.exists(state_file):
            os.unlink(state_file)
        assert should_send_alert("/", state_file, cooldown_hours=1) is True
        os.unlink(state_file)

    def test_suppresses_alert_in_cooldown(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"/": datetime.now().isoformat()}, f)
            state_file = f.name

        assert should_send_alert("/", state_file, cooldown_hours=1) is False
        os.unlink(state_file)

    def test_sends_alert_after_cooldown(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            old_time = (datetime.now() - timedelta(hours=2)).isoformat()
            json.dump({"/": old_time}, f)
            state_file = f.name

        assert should_send_alert("/", state_file, cooldown_hours=1) is True
        os.unlink(state_file)

    def test_different_mountpoints_tracked_independently(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            # "/" was alerted recently, "/data" was not
            json.dump({"/": datetime.now().isoformat()}, f)
            state_file = f.name

        assert should_send_alert("/",     state_file, cooldown_hours=1) is False
        assert should_send_alert("/data", state_file, cooldown_hours=1) is True
        os.unlink(state_file)

    def test_handles_corrupted_state_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json {{{{")
            state_file = f.name

        # should not crash — falls back to sending the alert
        result = should_send_alert("/", state_file, cooldown_hours=1)
        assert result is True
        os.unlink(state_file)


# ── health_check ──────────────────────────────────────────────────────────────
class TestCheckService:

    @patch("health_check.requests.get")
    def test_returns_up_on_200(self, mock_get):
        mock_resp          = MagicMock()
        mock_resp.status_code = 200
        mock_resp.elapsed.total_seconds.return_value = 0.1
        mock_get.return_value = mock_resp

        result = check_service("my-api", "http://localhost/health", timeout=5)
        assert result["status"]      == "UP"
        assert result["status_code"] == 200
        assert result["error"]       is None

    @patch("health_check.requests.get")
    def test_returns_down_on_500(self, mock_get):
        mock_resp             = MagicMock()
        mock_resp.status_code = 500
        mock_resp.elapsed.total_seconds.return_value = 0.2
        mock_get.return_value = mock_resp

        result = check_service("my-api", "http://localhost/health", timeout=5)
        assert result["status"] == "DOWN"
        assert result["error"]  is None

    @patch("health_check.requests.get")
    def test_returns_down_on_connection_error(self, mock_get):
        import requests as req
        mock_get.side_effect = req.exceptions.ConnectionError("refused")

        result = check_service("my-api", "http://localhost/health", timeout=5)
        assert result["status"] == "DOWN"
        assert "Connection" in result["error"]

    @patch("health_check.requests.get")
    def test_returns_down_on_timeout(self, mock_get):
        import requests as req
        mock_get.side_effect = req.exceptions.Timeout()

        result = check_service("my-api", "http://localhost/health", timeout=5)
        assert result["status"] == "DOWN"
        assert result["error"]  is not None

    @patch("health_check.requests.get")
    def test_includes_response_time(self, mock_get):
        mock_resp             = MagicMock()
        mock_resp.status_code = 200
        mock_resp.elapsed.total_seconds.return_value = 0.25
        mock_get.return_value = mock_resp

        result = check_service("my-api", "http://localhost/health", timeout=5)
        assert result["response_ms"] == 250


# ── cert_expiry_checker ───────────────────────────────────────────────────────
class TestCertBuildAlertMessage:

    def test_includes_critical_section(self):
        results = [
            {"hostname": "example.com", "port": 443, "status": "critical",
             "days_remaining": 3, "expiry": "2024-01-18 00:00:00 UTC", "error": None}
        ]
        msg = cert_build_alert(results, warn_days=30, critical_days=7)
        assert "CRITICAL" in msg
        assert "example.com" in msg
        assert "3 days" in msg

    def test_includes_warning_section(self):
        results = [
            {"hostname": "api.example.com", "port": 443, "status": "warning",
             "days_remaining": 20, "expiry": "2024-02-04 00:00:00 UTC", "error": None}
        ]
        msg = cert_build_alert(results, warn_days=30, critical_days=7)
        assert "WARNING" in msg
        assert "api.example.com" in msg

    def test_includes_error_section(self):
        results = [
            {"hostname": "broken.com", "port": 443, "status": "error",
             "days_remaining": None, "expiry": None, "error": "Connection refused"}
        ]
        msg = cert_build_alert(results, warn_days=30, critical_days=7)
        assert "CONNECTION ERRORS" in msg
        assert "broken.com" in msg
        assert "Connection refused" in msg

    def test_no_section_for_ok_results(self):
        results = [
            {"hostname": "ok.com", "port": 443, "status": "ok",
             "days_remaining": 90, "expiry": "2024-04-15 00:00:00 UTC", "error": None}
        ]
        msg = cert_build_alert(results, warn_days=30, critical_days=7)
        assert "CRITICAL" not in msg
        assert "WARNING"  not in msg


# ── process_monitor ───────────────────────────────────────────────────────────
class TestProcBuildAlertMessage:

    def test_includes_critical_process(self):
        results = [{
            "name":         "nginx",
            "status":       "critical",
            "instances":    [{"pid": 1234, "cpu_percent": 95.0, "mem_mb": 600.0}],
            "total_cpu":    95.0,
            "total_mem_mb": 600.0,
            "breaches":     ["Total CPU 95.0% exceeds threshold 80%"],
        }]
        msg = proc_build_alert(results, cpu_threshold=80, mem_threshold_mb=512)
        assert "nginx"     in msg
        assert "95.0%"     in msg
        assert "PID 1234"  in msg

    def test_includes_not_found_process(self):
        results = [{
            "name":         "myapp",
            "status":       "not_found",
            "instances":    [],
            "total_cpu":    0,
            "total_mem_mb": 0,
            "breaches":     ["Process not running"],
        }]
        msg = proc_build_alert(results, cpu_threshold=80, mem_threshold_mb=512)
        assert "myapp"     in msg
        assert "not found" in msg


# ── runbook_runner ────────────────────────────────────────────────────────────
class TestRenderTemplate:

    def test_replaces_single_variable(self):
        result = render_template("Deploy to {{ env }}", {"env": "prod"})
        assert result == "Deploy to prod"

    def test_replaces_multiple_variables(self):
        result = render_template(
            "{{ service }} on {{ hostname }}",
            {"service": "nginx", "hostname": "web-01"}
        )
        assert result == "nginx on web-01"

    def test_leaves_unknown_variables_intact(self):
        result = render_template("Hello {{ unknown }}", {})
        assert result == "Hello {{ unknown }}"

    def test_handles_spaces_around_variable_name(self):
        result = render_template("{{ env }}", {"env": "staging"})
        assert result == "staging"

    def test_empty_string(self):
        assert render_template("", {"env": "prod"}) == ""

    def test_no_placeholders(self):
        assert render_template("plain text", {"env": "prod"}) == "plain text"


class TestRenderStep:

    def test_renders_string_values(self):
        step   = {"name": "Deploy {{ service }}", "type": "slack"}
        result = render_step(step, {"service": "payments"})
        assert result["name"] == "Deploy payments"

    def test_leaves_non_string_values_unchanged(self):
        step   = {"name": "Check", "timeout": 30, "retries": 2}
        result = render_step(step, {})
        assert result["timeout"] == 30
        assert result["retries"] == 2

    def test_renders_nested_dict(self):
        step   = {"name": "Check", "headers": {"X-Env": "{{ env }}"}}
        result = render_step(step, {"env": "prod"})
        assert result["headers"]["X-Env"] == "prod"


# ── alert ─────────────────────────────────────────────────────────────────────
class TestSendSlack:

    @patch("alert.requests.post")
    def test_sends_slack_message_successfully(self, mock_post):
        from alert import send_slack
        mock_post.return_value = MagicMock(status_code=200)

        result = send_slack("https://hooks.slack.com/test", "hello")
        assert result is True
        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs["json"]
        assert payload["text"] == "hello"

    @patch("alert.requests.post")
    def test_returns_false_on_non_200(self, mock_post):
        from alert import send_slack
        mock_post.return_value = MagicMock(status_code=500)

        result = send_slack("https://hooks.slack.com/test", "hello")
        assert result is False

    @patch("alert.requests.post")
    def test_returns_false_on_connection_error(self, mock_post):
        import requests as req
        from alert import send_slack
        mock_post.side_effect = req.exceptions.ConnectionError()

        result = send_slack("https://hooks.slack.com/test", "hello")
        assert result is False

    def test_returns_false_when_no_webhook_url(self):
        from alert import send_slack
        result = send_slack("", "hello")
        assert result is False

    def test_returns_false_when_webhook_url_is_none(self):
        from alert import send_slack
        result = send_slack(None, "hello")
        assert result is False


class TestSendEmail:

    @patch("alert.smtplib.SMTP")
    def test_sends_email_successfully(self, mock_smtp):
        from alert import send_email
        config = {
            "smtp_user":     "user@example.com",
            "smtp_password": "secret",
            "smtp_host":     "smtp.gmail.com",
            "smtp_port":     587,
            "alert_from":    "alerts@example.com",
            "alert_to":      "oncall@example.com",
        }
        mock_server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_server

        result = send_email(config, "Test subject", "Test body")
        assert result is True
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("user@example.com", "secret")
        mock_server.send_message.assert_called_once()

    def test_returns_false_when_no_credentials(self):
        from alert import send_email
        result = send_email({}, "Subject", "Body")
        assert result is False

    @patch("alert.smtplib.SMTP")
    def test_returns_false_on_auth_failure(self, mock_smtp):
        import smtplib
        from alert import send_email
        mock_smtp.return_value.__enter__.side_effect = smtplib.SMTPAuthenticationError(535, "Bad credentials")
        config = {
            "smtp_user":     "user@example.com",
            "smtp_password": "wrongpassword",
        }
        result = send_email(config, "Subject", "Body")
        assert result is False


class TestSendAlert:

    @patch("alert.send_slack")
    @patch("alert.send_email")
    def test_calls_both_channels(self, mock_email, mock_slack):
        from alert import send_alert
        config = {
            "alerts": {
                "slack_webhook_url": "https://hooks.slack.com/test",
                "smtp_user":         "user@example.com",
                "smtp_password":     "secret",
            }
        }
        send_alert(config, "Test alert", "Something went wrong")
        mock_slack.assert_called_once()
        mock_email.assert_called_once()

    @patch("alert.send_slack")
    @patch("alert.send_email")
    def test_subject_included_in_slack_message(self, mock_email, mock_slack):
        from alert import send_alert
        config = {"alerts": {"slack_webhook_url": "https://hooks.slack.com/test"}}
        send_alert(config, "DISK ALERT", "/ is 90% full")

        slack_message = mock_slack.call_args[0][1]
        assert "DISK ALERT"   in slack_message
        assert "/ is 90% full" in slack_message


# ── k8s_pod_restarts ──────────────────────────────────────────────────────────
class TestGetLastStateReason:

    def test_returns_reason_and_exit_code(self):
        from k8s_pod_restarts import get_last_state_reason
        cs            = MagicMock()
        cs.last_state.terminated.reason    = "OOMKilled"
        cs.last_state.terminated.exit_code = 137
        assert get_last_state_reason(cs) == "OOMKilled (exit 137)"

    def test_returns_unknown_when_no_last_state(self):
        from k8s_pod_restarts import get_last_state_reason
        cs            = MagicMock()
        cs.last_state = None
        assert get_last_state_reason(cs) == "unknown"

    def test_returns_unknown_when_not_terminated(self):
        from k8s_pod_restarts import get_last_state_reason
        cs                           = MagicMock()
        cs.last_state.terminated     = None
        assert get_last_state_reason(cs) == "unknown"


class TestCheckNamespace:

    def test_returns_issues_for_pods_exceeding_threshold(self):
        from k8s_pod_restarts import check_namespace

        # build a fake pod with one container that has 10 restarts
        container_status               = MagicMock()
        container_status.restart_count = 10
        container_status.ready         = False
        container_status.name          = "app"
        container_status.last_state    = None

        pod                              = MagicMock()
        pod.metadata.name                = "payments-abc123"
        pod.metadata.namespace           = "production"
        pod.status.phase                 = "Running"
        pod.status.container_statuses    = [container_status]

        v1               = MagicMock()
        v1.list_namespaced_pod.return_value = MagicMock(items=[pod])

        issues = check_namespace(v1, "production", threshold=5)
        assert len(issues) == 1
        assert issues[0]["pod"]      == "payments-abc123"
        assert issues[0]["restarts"] == 10

    def test_ignores_pods_below_threshold(self):
        from k8s_pod_restarts import check_namespace

        container_status               = MagicMock()
        container_status.restart_count = 2
        container_status.ready         = True
        container_status.name          = "app"
        container_status.last_state    = None

        pod                           = MagicMock()
        pod.metadata.name             = "healthy-pod"
        pod.metadata.namespace        = "default"
        pod.status.phase              = "Running"
        pod.status.container_statuses = [container_status]

        v1 = MagicMock()
        v1.list_namespaced_pod.return_value = MagicMock(items=[pod])

        issues = check_namespace(v1, "default", threshold=5)
        assert issues == []

    def test_returns_empty_list_on_api_error(self):
        from k8s_pod_restarts import check_namespace
        from kubernetes.client.exceptions import ApiException

        v1 = MagicMock()
        v1.list_namespaced_pod.side_effect = ApiException(status=403, reason="Forbidden")

        issues = check_namespace(v1, "restricted", threshold=5)
        assert issues == []

    def test_skips_pods_with_no_container_statuses(self):
        from k8s_pod_restarts import check_namespace

        pod                           = MagicMock()
        pod.metadata.name             = "pending-pod"
        pod.metadata.namespace        = "default"
        pod.status.phase              = "Pending"
        pod.status.container_statuses = None

        v1 = MagicMock()
        v1.list_namespaced_pod.return_value = MagicMock(items=[pod])

        issues = check_namespace(v1, "default", threshold=5)
        assert issues == []


class TestK8sBuildAlertMessage:

    def test_groups_issues_by_namespace(self):
        from k8s_pod_restarts import build_alert_message
        issues = [
            {"namespace": "production", "pod": "api-abc",  "container": "app",
             "restarts": 10, "ready": False, "phase": "Running", "last_state": "OOMKilled (exit 137)"},
            {"namespace": "staging",    "pod": "web-xyz",  "container": "nginx",
             "restarts": 6,  "ready": True,  "phase": "Running", "last_state": "unknown"},
        ]
        msg = build_alert_message(issues, threshold=5)
        assert "production" in msg
        assert "staging"    in msg
        assert "api-abc"    in msg
        assert "web-xyz"    in msg

    def test_includes_restart_count(self):
        from k8s_pod_restarts import build_alert_message
        issues = [
            {"namespace": "default", "pod": "crasher", "container": "app",
             "restarts": 42, "ready": False, "phase": "Running", "last_state": "Error (exit 1)"},
        ]
        msg = build_alert_message(issues, threshold=5)
        assert "42" in msg

    def test_includes_total_count(self):
        from k8s_pod_restarts import build_alert_message
        issues = [
            {"namespace": "default", "pod": "pod-1", "container": "app",
             "restarts": 8, "ready": False, "phase": "Running", "last_state": "unknown"},
            {"namespace": "default", "pod": "pod-2", "container": "app",
             "restarts": 9, "ready": False, "phase": "Running", "last_state": "unknown"},
        ]
        msg = build_alert_message(issues, threshold=5)
        assert "2" in msg


# ── runbook_runner step executors ─────────────────────────────────────────────
class TestRunShellStep:

    def test_dry_run_returns_true_without_executing(self):
        from runbook_runner import run_shell_step
        ok, detail = run_shell_step({"command": "rm -rf /"}, dry_run=True)
        assert ok     is True
        assert detail == "dry-run"

    def test_successful_command_returns_true(self):
        from runbook_runner import run_shell_step
        ok, detail = run_shell_step({"command": "echo hello"}, dry_run=False)
        assert ok     is True
        assert "hello" in detail

    def test_failing_command_returns_false(self):
        from runbook_runner import run_shell_step
        ok, detail = run_shell_step({"command": "false"}, dry_run=False)
        assert ok is False

    def test_retries_on_failure(self):
        from runbook_runner import run_shell_step
        # "false" always fails — with 2 retries it should attempt 3 times total
        ok, detail = run_shell_step({"command": "false", "retries": 2}, dry_run=False)
        assert ok is False
        assert "3 attempt" in detail

    def test_nonexistent_command_returns_false(self):
        from runbook_runner import run_shell_step
        ok, detail = run_shell_step({"command": "nonexistent_cmd_xyz"}, dry_run=False)
        assert ok is False
        assert "not found" in detail.lower()


class TestRunHttpStep:

    @patch("runbook_runner.requests.request")
    def test_returns_true_on_expected_status(self, mock_req):
        from runbook_runner import run_http_step
        mock_resp             = MagicMock()
        mock_resp.status_code = 200
        mock_resp.elapsed.total_seconds.return_value = 0.1
        mock_req.return_value = mock_resp

        ok, detail = run_http_step(
            {"url": "http://localhost/health", "expected_status": 200},
            dry_run=False
        )
        assert ok is True
        assert "200" in detail

    @patch("runbook_runner.requests.request")
    def test_returns_false_on_unexpected_status(self, mock_req):
        from runbook_runner import run_http_step
        mock_resp             = MagicMock()
        mock_resp.status_code = 503
        mock_resp.elapsed.total_seconds.return_value = 0.1
        mock_req.return_value = mock_resp

        ok, _ = run_http_step(
            {"url": "http://localhost/health", "expected_status": 200},
            dry_run=False
        )
        assert ok is False

    @patch("runbook_runner.requests.request")
    def test_returns_false_on_timeout(self, mock_req):
        import requests as req
        from runbook_runner import run_http_step
        mock_req.side_effect = req.exceptions.Timeout()

        ok, _ = run_http_step({"url": "http://localhost/health"}, dry_run=False)
        assert ok is False

    def test_dry_run_returns_true_without_calling(self):
        from runbook_runner import run_http_step
        ok, detail = run_http_step(
            {"url": "http://localhost/health", "expected_status": 200},
            dry_run=True
        )
        assert ok     is True
        assert detail == "dry-run"


class TestRunSleepStep:

    @patch("runbook_runner.time.sleep")
    def test_sleeps_for_configured_seconds(self, mock_sleep):
        from runbook_runner import run_sleep_step
        ok, detail = run_sleep_step({"seconds": 5}, dry_run=False)
        assert ok is True
        mock_sleep.assert_called_once_with(5)

    def test_dry_run_skips_sleep(self):
        from runbook_runner import run_sleep_step
        ok, detail = run_sleep_step({"seconds": 60}, dry_run=True)
        assert ok     is True
        assert detail == "dry-run"


class TestRunRunbook:

    def test_all_steps_pass_returns_success(self):
        from runbook_runner import run_runbook
        runbook = {
            "name": "Test runbook",
            "steps": [
                {"name": "Step 1", "type": "shell", "command": "echo ok"},
                {"name": "Step 2", "type": "sleep", "seconds": 0},
            ]
        }
        summary = run_runbook(runbook, config={}, variables={}, dry_run=True)
        assert summary["success"] is True
        assert summary["failed"]  == 0

    def test_failed_step_aborts_runbook(self):
        from runbook_runner import run_runbook
        runbook = {
            "name":       "Abort test",
            "on_failure": "abort",
            "steps": [
                {"name": "Fail step",   "type": "shell", "command": "false"},
                {"name": "Never runs",  "type": "shell", "command": "echo should not run"},
            ]
        }
        summary = run_runbook(runbook, config={}, variables={}, dry_run=False)
        assert summary["success"]             is False
        assert summary["aborted"]             is True
        assert summary["steps"][1]["status"]  == "skipped"

    def test_on_failure_continue_does_not_abort(self):
        from runbook_runner import run_runbook
        runbook = {
            "name":       "Continue test",
            "on_failure": "abort",
            "steps": [
                {"name": "Fail but continue", "type": "shell",
                 "command": "false", "on_failure": "continue"},
                {"name": "Still runs",        "type": "shell", "command": "echo ok"},
            ]
        }
        summary = run_runbook(runbook, config={}, variables={}, dry_run=False)
        assert summary["aborted"]            is False
        assert summary["steps"][1]["status"] == "ok"

    def test_variables_are_substituted_in_steps(self):
        from runbook_runner import run_runbook
        runbook = {
            "name": "Template test",
            "steps": [
                {"name": "Echo env", "type": "shell", "command": "echo {{ env }}"},
            ]
        }
        summary = run_runbook(
            runbook, config={}, variables={"env": "prod"}, dry_run=False
        )
        assert summary["success"] is True

    def test_dry_run_succeeds_without_executing(self):
        from runbook_runner import run_runbook
        runbook = {
            "name": "Dry run test",
            "steps": [
                {"name": "Dangerous", "type": "shell", "command": "rm -rf /"},
                {"name": "HTTP",      "type": "http",  "url": "http://localhost/health"},
                {"name": "Sleep",     "type": "sleep", "seconds": 100},
            ]
        }
        summary = run_runbook(runbook, config={}, variables={}, dry_run=True)
        assert summary["success"] is True
        assert summary["ok"]      == 3