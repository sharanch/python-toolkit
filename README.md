# sre-toolkit 🛠️

A collection of production-grade Python scripts for SRE automation.

---

## Scripts

| Script | What it does |
|--------|-------------|
| `log_parser.py` | Parse log files, count log levels, extract errors |
| `disk_monitor.py` | Monitor disk usage and alert when thresholds are exceeded |
| `health_check.py` | HTTP health checks with response time tracking |
| `cert_expiry_checker.py` | TLS certificate expiry monitoring with warn/critical thresholds |
| `k8s_pod_restarts.py` | Detect crashlooping pods in Kubernetes clusters |
| `process_monitor.py` | Monitor CPU and memory usage of specific processes |
| `runbook_runner.py` | Execute YAML-defined incident response runbooks |
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

### Check TLS certificate expiry
```bash
python scripts/cert_expiry_checker.py --domains example.com api.example.com
python scripts/cert_expiry_checker.py --warn-days 30 --critical-days 7
```

### Detect crashlooping Kubernetes pods
```bash
python scripts/k8s_pod_restarts.py --namespace production --threshold 5
python scripts/k8s_pod_restarts.py --all-namespaces
```

### Monitor process CPU/memory
```bash
python scripts/process_monitor.py --processes nginx postgres
python scripts/process_monitor.py --pid 1234 --cpu-threshold 80
```

### Run a YAML runbook
```bash
python scripts/runbook_runner.py --runbook runbooks/restart_service.yaml
python scripts/runbook_runner.py --runbook runbooks/drain_node.yaml --dry-run
python scripts/runbook_runner.py --runbook runbooks/deploy.yaml --var env=prod
```

---

## Configuration

Edit `config/config.json` to configure thresholds, services, and alert channels.
All scripts read from this single config file — no per-script config needed.

```json
{
    "disk":            { "threshold_percent": 50, "cooldown_hours": 1 },
    "health_check":    { "timeout_seconds": 5, "services": [...] },
    "cert_checker":    { "warn_days": 30, "critical_days": 7, "domains": [...] },
    "k8s_monitor":     { "restart_threshold": 5, "namespaces": [...] },
    "process_monitor": { "cpu_threshold": 80, "mem_threshold_mb": 512, "processes": [...] },
    "alerts": {
        "slack_webhook_url": "https://hooks.slack.com/...",
        "smtp_user": "alerts@company.com",
        "alert_to": "oncall@company.com"
    }
}
```

---

## Alerting

All scripts share a single `alert.py` module — configure once, works everywhere.

- **Slack** — set `slack_webhook_url` in config
- **Email** — set SMTP credentials in config
- **`--no-alert`** flag available on every script to suppress alerts during testing

Disk monitor includes **cooldown logic** to prevent alert fatigue.

---

## Cron Setup

```bash
# Log errors every 5 minutes
*/5 * * * * python /opt/sre-toolkit/scripts/log_parser.py /var/log/app/server.log

# Disk check every 5 minutes
*/5 * * * * python /opt/sre-toolkit/scripts/disk_monitor.py

# Health check every minute
* * * * * python /opt/sre-toolkit/scripts/health_check.py

# Cert expiry check daily
0 9 * * * python /opt/sre-toolkit/scripts/cert_expiry_checker.py

# Pod restart check every 5 minutes
*/5 * * * * python /opt/sre-toolkit/scripts/k8s_pod_restarts.py --all-namespaces

# Process monitor every 2 minutes
*/2 * * * * python /opt/sre-toolkit/scripts/process_monitor.py
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
│   ├── alert.py
│   ├── log_parser.py
│   ├── disk_monitor.py
│   ├── health_check.py
│   ├── cert_expiry_checker.py
│   ├── k8s_pod_restarts.py
│   ├── process_monitor.py
│   └── runbook_runner.py
├── tests/
│   └── test_scripts.py
└── docs/
    └── runbook.md
```