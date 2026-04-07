# SRE Toolkit — Runbook

Operational guide for using each script during oncall and day-to-day SRE work.

---

## log_parser.py

**When to use:** Service is throwing errors, you need a quick summary of what's happening in the logs.

```bash
python scripts/log_parser.py /var/log/app/server.log
```

**Exit codes:** `0` = no errors found, `1` = errors or criticals found

**Cron example:**
```bash
*/5 * * * * python /opt/sre-toolkit/scripts/log_parser.py /var/log/app/server.log
```

---

## disk_monitor.py

**When to use:** Disk usage alert fired, or proactively check before a big deploy.

```bash
python scripts/disk_monitor.py
python scripts/disk_monitor.py --threshold 80
```

**Exit codes:** `0` = all disks healthy, `1` = threshold exceeded

**Alert cooldown:** Suppresses repeat alerts per mountpoint for 1 hour (configurable).

**Cron example:**
```bash
*/5 * * * * python /opt/sre-toolkit/scripts/disk_monitor.py
```

---

## health_check.py

**When to use:** Verify all services are up after a deploy, or check a single endpoint.

```bash
python scripts/health_check.py
python scripts/health_check.py --url https://myservice.com/health
python scripts/health_check.py --timeout 10
```

**Exit codes:** `0` = all UP, `1` = one or more DOWN

**Cron example:**
```bash
* * * * * python /opt/sre-toolkit/scripts/health_check.py
```

---

## cert_expiry_checker.py

**When to use:** Proactively check TLS cert expiry before it causes an outage.

```bash
python scripts/cert_expiry_checker.py
python scripts/cert_expiry_checker.py --domains example.com api.example.com
python scripts/cert_expiry_checker.py --warn-days 30 --critical-days 7
```

**Exit codes:** `0` = all OK, `1` = warning, `2` = critical or connection error

**Cron example** (daily at 9am):
```bash
0 9 * * * python /opt/sre-toolkit/scripts/cert_expiry_checker.py
```

---

## k8s_pod_restarts.py

**When to use:** CrashLoopBackOff alert fired, or routine check for pod instability.

```bash
python scripts/k8s_pod_restarts.py --namespace production
python scripts/k8s_pod_restarts.py --all-namespaces --threshold 3
```

**Exit codes:** `0` = no issues, `1` = pods exceeding restart threshold

**Cron example:**
```bash
*/5 * * * * python /opt/sre-toolkit/scripts/k8s_pod_restarts.py --all-namespaces
```

---

## process_monitor.py

**When to use:** Service is slow or using too many resources; check CPU/memory per process.

```bash
python scripts/process_monitor.py --processes nginx postgres
python scripts/process_monitor.py --pid 1234 --cpu-threshold 80
python scripts/process_monitor.py --processes nginx --samples 5 --interval 2
```

**Exit codes:** `0` = all within thresholds, `1` = threshold exceeded or process not found

**Cron example:**
```bash
*/2 * * * * python /opt/sre-toolkit/scripts/process_monitor.py
```

---

## runbook_runner.py

**When to use:** Executing a defined operational procedure — deployments, restarts, incident response.

```bash
# Run a runbook
python scripts/runbook_runner.py --runbook runbooks/restart_nginx.yaml

# Dry run — see what would happen without executing
python scripts/runbook_runner.py --runbook runbooks/restart_nginx.yaml --dry-run

# Pass variables for template substitution
python scripts/runbook_runner.py --runbook runbooks/pre_deploy_checks.yaml \
  --var service=payments --var env=prod

# Save JSON results for audit trail
python scripts/runbook_runner.py --runbook runbooks/restart_nginx.yaml \
  --var hostname=web-01 --var env=prod \
  --output results/restart_nginx_$(date +%Y%m%d_%H%M%S).json
```

**Exit codes:** `0` = all steps passed, `1` = one or more steps failed

---

## Available Runbooks

### `runbooks/pre_deploy_checks.yaml`
Run before every deployment to verify dependencies are healthy.

**Variables required:**
- `{{ service }}` — name of the service being deployed
- `{{ env }}` — environment e.g. `prod`, `staging`

```bash
python scripts/runbook_runner.py \
  --runbook runbooks/pre_deploy_checks.yaml \
  --var service=payments-api \
  --var env=prod
```

**Steps:**
1. Slack notification — starting checks
2. HTTP — database connectivity (`/readyz/db`)
3. HTTP — Redis connectivity (`/readyz/redis`)
4. HTTP — upstream API health
5. Shell — disk space check (aborts if > 85%)
6. Shell — load average check (continues even if high)
7. Slack notification — all clear

---

### `runbooks/restart_nginx.yaml`
Safely reload nginx config with pre/post verification.

**Variables required:**
- `{{ hostname }}` — server hostname for Slack notifications
- `{{ env }}` — environment label

```bash
python scripts/runbook_runner.py \
  --runbook runbooks/restart_nginx.yaml \
  --var hostname=web-01.prod \
  --var env=prod
```

**Steps:**
1. Slack notification — starting reload
2. Shell — `nginx -t` config test (aborts if invalid)
3. Shell — `systemctl reload nginx`
4. Sleep — 5s for workers to cycle
5. HTTP — health check on `/health`
6. Shell — `pgrep -x nginx` process check (continues on failure)
7. Slack notification — reload complete

---

## Writing Your Own Runbooks

Runbooks are YAML files in the `runbooks/` directory. Each step supports:

| Field | Description |
|-------|-------------|
| `name` | Human readable step name |
| `type` | `shell`, `http`, `sleep`, or `slack` |
| `on_failure` | `abort` (default) or `continue` |
| `retries` | Number of retry attempts |
| `timeout` | Seconds before giving up |

Use `{{ variable }}` placeholders anywhere in string values — pass values with `--var key=value`.

Always test with `--dry-run` before running in production.