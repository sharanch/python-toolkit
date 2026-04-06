# SRE Toolkit — Runbook

Operational guide for using each script during oncall and day-to-day SRE work.

---

## log_parser.py

**When to use:** Service is throwing errors, you need a quick summary of what's happening in the logs.

```bash
python scripts/log_parser.py /var/log/app/server.log
```

**Exit codes:**
- `0` — No errors found
- `1` — Errors or criticals found in log

**Cron example** — alert if errors found:
```bash
*/5 * * * * python /opt/sre-toolkit/scripts/log_parser.py /var/log/app/server.log
```

---

## disk_monitor.py

**When to use:** Disk usage alert fired, or proactively check before a big deploy.

```bash
# use threshold from config.json
python scripts/disk_monitor.py

# override threshold on the fly
python scripts/disk_monitor.py --threshold 80
```

**Exit codes:**
- `0` — All disks healthy
- `1` — One or more disks exceeded threshold

**Cron example** — check every 5 minutes:
```bash
*/5 * * * * python /opt/sre-toolkit/scripts/disk_monitor.py
```

**Alert cooldown:** Alerts are suppressed per mountpoint for 1 hour (configurable in config.json) to prevent spam.

---

## health_check.py

**When to use:** Service is down, or proactively verify all services are up after a deploy.

```bash
# check all services from config.json
python scripts/health_check.py

# check a single URL
python scripts/health_check.py --url https://myservice.com/health

# custom timeout
python scripts/health_check.py --timeout 10
```

**Exit codes:**
- `0` — All services UP
- `1` — One or more services DOWN

**Cron example** — check every minute:
```bash
* * * * * python /opt/sre-toolkit/scripts/health_check.py
```

---

## Configuration

Edit `config/config.json` to configure:
- Disk usage threshold and cooldown
- Services to health check
- Slack webhook URL for alerts
- SMTP credentials for email alerts

---

## Setup

```bash
git clone https://github.com/yourname/sre-toolkit
cd sre-toolkit
pip install -r requirements.txt

# run tests
python -m pytest tests/ -v
```
