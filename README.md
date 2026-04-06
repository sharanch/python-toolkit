# sre-toolkit 🛠️

A collection of production-grade Python scripts for SRE automation — log parsing, disk monitoring, and service health checks.

---

## Scripts

| Script | What it does |
|--------|-------------|
| `log_parser.py` | Parse log files, count log levels, extract errors |
| `disk_monitor.py` | Monitor disk usage and alert when thresholds are exceeded |
| `health_check.py` | HTTP health checks for services with response time tracking |
| `alert.py` | Shared alerting via Slack webhook and SMTP email |

---

## Quick Start

```bash
git clone https://github.com/yourname/sre-toolkit
cd sre-toolkit
pip install -r requirements.txt
```

### Parse a log file
```bash
python scripts/log_parser.py /var/log/app/server.log
```

### Check disk usage
```bash
python scripts/disk_monitor.py --threshold 80
```

### Check service health
```bash
python scripts/health_check.py --url https://myservice.com/health
```

---

## Configuration

Edit `config/config.json`:

```json
{
    "disk": {
        "threshold_percent": 50,
        "cooldown_hours": 1
    },
    "health_check": {
        "timeout_seconds": 5,
        "services": [
            {"name": "my-api", "url": "https://myapi.com/health"}
        ]
    },
    "alerts": {
        "slack_webhook_url": "https://hooks.slack.com/...",
        "smtp_user": "alerts@company.com",
        "smtp_password": "yourpassword",
        "alert_to": "oncall@company.com"
    }
}
```

---

## Alerting

Supports two alert channels out of the box:

- **Slack** — set `slack_webhook_url` in config
- **Email** — set SMTP credentials in config

Disk monitor includes **cooldown logic** to prevent alert fatigue — each mountpoint is only alerted once per hour by default.

---

## Cron Setup

```bash
# Check disk every 5 minutes
*/5 * * * * python /opt/sre-toolkit/scripts/disk_monitor.py

# Health check every minute
* * * * * python /opt/sre-toolkit/scripts/health_check.py

# Parse logs every 5 minutes
*/5 * * * * python /opt/sre-toolkit/scripts/log_parser.py /var/log/app/server.log
```

---

## Running Tests

```bash
python -m pytest tests/ -v
```

---

## Project Structure

```
sre-toolkit/
├── README.md
├── requirements.txt
├── config/
│   └── config.json
├── scripts/
│   ├── log_parser.py
│   ├── disk_monitor.py
│   ├── health_check.py
│   └── alert.py
├── tests/
│   └── test_scripts.py
└── docs/
    └── runbook.md
```
