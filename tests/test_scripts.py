"""
test_scripts.py — Unit tests for sre-toolkit scripts
Run with: python -m pytest tests/ -v
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../scripts"))

from log_parser import parse_log_line, summarize
from disk_monitor import should_send_alert
import tempfile
import json


# ── log_parser tests ──────────────────────────────────────────────────────────
class TestParseLogLine:

    def test_parses_valid_error_line(self):
        line = "2024-01-15 08:25:03 ERROR Disk usage exceeded 90%"
        result = parse_log_line(line)
        assert result["date"]    == "2024-01-15"
        assert result["time"]    == "08:25:03"
        assert result["level"]   == "ERROR"
        assert result["message"] == "Disk usage exceeded 90%"

    def test_parses_valid_info_line(self):
        line = "2024-01-15 08:26:45 INFO Request handled successfully"
        result = parse_log_line(line)
        assert result["level"]   == "INFO"
        assert result["message"] == "Request handled successfully"

    def test_returns_none_for_malformed_line(self):
        line = "this is not a valid log line"
        result = parse_log_line(line)
        assert result is None

    def test_returns_none_for_empty_line(self):
        result = parse_log_line("")
        assert result is None


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
        counts = summarize([])
        assert counts == {}


# ── disk_monitor tests ────────────────────────────────────────────────────────
class TestShouldSendAlert:

    def test_sends_alert_first_time(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=True) as f:
            state_file = f.name

        # file doesn't exist yet — should send
        result = should_send_alert("/", state_file, cooldown_hours=1)
        assert result is True

    def test_suppresses_alert_in_cooldown(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            from datetime import datetime
            state = {"/": datetime.now().isoformat()}
            json.dump(state, f)
            state_file = f.name

        # alert was just sent — should suppress
        result = should_send_alert("/", state_file, cooldown_hours=1)
        assert result is False

        os.unlink(state_file)

    def test_sends_alert_after_cooldown(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            from datetime import datetime, timedelta
            # set last alert to 2 hours ago
            old_time = (datetime.now() - timedelta(hours=2)).isoformat()
            state = {"/": old_time}
            json.dump(state, f)
            state_file = f.name

        # cooldown has passed — should send
        result = should_send_alert("/", state_file, cooldown_hours=1)
        assert result is True

        os.unlink(state_file)
