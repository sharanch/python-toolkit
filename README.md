# python-toolkit 🛠️

A collection of production-grade Python scripts for SRE automation — log parsing, disk monitoring, service health checks, TLS certificate management, process monitoring, Kubernetes observability, and runbook automation.

---

## Scripts

| Script | What it does |
| --- | --- |
| `log_parser.py` | Parse log files, count log levels, extract errors |
| `disk_monitor.py` | Monitor disk usage and alert when thresholds are exceeded |
| `health_check.py` | HTTP health checks for services with response time tracking |
| `alert.py` | Shared alerting via Slack webhook and SMTP email |
| `cert_expiry_checker.py` | Check TLS certificate expiry, alert on warn/critical thresholds |
| `process_monitor.py` | Monitor CPU/memory of named processes, alert on threshold breach |
| `k8s_pod_restarts.py` | Detect crashlooping Kubernetes pods across namespaces |
| `runbook_runner.py` | Execute YAML-defined runbooks: shell, HTTP, Slack, sleep steps |

---

## Quick Start

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
# Check domains from config
python scripts/cert_expiry_checker.py

# Override domains on the CLI
python scripts/cert_expiry_checker.py --domains example.com api.example.com

# Custom thresholds (warn at 14 days, critical at 3 days)
python scripts/cert_expiry_checker.py --warn-days 14 --critical-days 3

# Just print results, no alert
python scripts/cert_expiry_checker.py --no-alert
```

### Monitor process CPU/memory
```bash
# Monitor processes from config
python scripts/process_monitor.py

# Override on CLI
python scripts/process_monitor.py --processes nginx postgres redis-server

# Custom thresholds
python scripts/process_monitor.py --processes nginx --cpu-threshold 70 --mem-threshold 256

# Monitor a specific PID
python scripts/process_monitor.py --pid 1234

# Average over 5 samples, 2s apart (smooths spikes)
python scripts/process_monitor.py --processes nginx --samples 5 --interval 2
```

### Detect crashlooping Kubernetes pods
```bash
# Check default namespace
python scripts/k8s_pod_restarts.py

# Check a specific namespace with custom threshold
python scripts/k8s_pod_restarts.py --namespace production --threshold 3

# Check all namespaces
python scripts/k8s_pod_restarts.py --all-namespaces --threshold 5

# Use a specific kubeconfig
python scripts/k8s_pod_restarts.py --kubeconfig ~/.kube/prod-config

# Running inside a pod (uses service account)
python scripts/k8s_pod_restarts.py --in-cluster
```

### Run a runbook
```bash
# Dry run first (always recommended)
python scripts/runbook_runner.py --runbook runbooks/restart_nginx.yaml --dry-run

# Execute with template variables
python scripts/runbook_runner.py \
  --runbook runbooks/restart_nginx.yaml \
  --var hostname=web-01 \
  --var env=production

# Write JSON results to file
python scripts/runbook_runner.py \
  --runbook runbooks/pre_deploy_checks.yaml \
  --var env=staging \
  --output /tmp/runbook-results.json
```

---

## Runbook Format

Runbooks are YAML files in the `runbooks/` directory. Each step has a `type` of `shell`, `http`, `sleep`, or `slack`. Steps support `retries`, `timeout`, and per-step `on_failure` overrides.

```yaml
name: Restart nginx and verify
description: Reload nginx config, wait, then confirm health
on_failure: abort   # abort (default) | continue

steps:
  - name: Notify team
    type: slack
    message: "Starting nginx reload on {{ hostname }}"

  - name: Test config
    type: shell
    command: nginx -t
    timeout: 15

  - name: Reload
    type: shell
    command: systemctl reload nginx
    timeout: 30
    retries: 1

  - name: Wait
    type: sleep
    seconds: 5

  - name: Health check
    type: http
    url: http://localhost/health
    expected_status: 200
    timeout: 10
    retries: 3

  - name: Confirm process
    type: shell
    command: pgrep -x nginx
    on_failure: continue   # don't abort runbook if this fails
```

Template variables (`{{ var }}`) are substituted from `--var key=value` CLI args.

---

## Configuration

Edit `config/config.json`:

```json
{
    "disk": {
        "threshold_percent": 80,
        "cooldown_hours": 1
    },
    "health_check": {
        "timeout_seconds": 5,
        "services": [
            {"name": "my-api", "url": "https://myapi.com/health"}
        ]
    },
    "cert_checker": {
        "warn_days": 30,
        "critical_days": 7,
        "domains": ["example.com", "api.example.com"]
    },
    "process_monitor": {
        "cpu_threshold": 80.0,
        "mem_threshold_mb": 512,
        "processes": ["nginx", "postgres", "redis-server"]
    },
    "k8s_monitor": {
        "restart_threshold": 5,
        "namespaces": ["default", "production"]
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

Disk monitor includes **cooldown logic** to prevent alert fatigue. All scripts accept `--no-alert` to suppress alerts for manual/CI runs.

All scripts exit with a non-zero code on failures, making them safe to use in CI pipelines and cron.

---

## Cron Setup

```bash
# Check disk every 5 minutes
*/5 * * * * python /opt/sre-toolkit/scripts/disk_monitor.py

# Health check every minute
* * * * * python /opt/sre-toolkit/scripts/health_check.py

# Parse logs every 5 minutes
*/5 * * * * python /opt/sre-toolkit/scripts/log_parser.py /var/log/app/server.log

# Check TLS certs daily at 8am
0 8 * * * python /opt/sre-toolkit/scripts/cert_expiry_checker.py

# Monitor critical process CPU/mem every minute
* * * * * python /opt/sre-toolkit/scripts/process_monitor.py --processes nginx postgres

# Check for crashlooping pods every 2 minutes
*/2 * * * * python /opt/sre-toolkit/scripts/k8s_pod_restarts.py --all-namespaces
```

---

## Dependencies

```bash
pip install -r requirements.txt
```

New dependencies for extended scripts:
- `psutil` — process CPU/memory monitoring
- `kubernetes` — Kubernetes API client
- `pyyaml` — runbook YAML parsing
- `requests` — HTTP steps in runbook runner (already used by health_check)

---

## Running Tests

```bash
python -m pytest tests/ -v
```

---

## Project Structure

```
python-toolkit/
├── README.md
├── requirements.txt
├── config/
│   └── config.json
├── runbooks/
│   ├── restart_nginx.yaml
│   └── pre_deploy_checks.yaml
├── scripts/
│   ├── alert.py
│   ├── log_parser.py
│   ├── disk_monitor.py
│   ├── health_check.py
│   ├── cert_expiry_checker.py
│   ├── process_monitor.py
│   ├── k8s_pod_restarts.py
│   └── runbook_runner.py
├── tests/
│   ├── test_scripts.py
│   └── test_new_scripts.py
└── docs/
    └── runbook.md
```