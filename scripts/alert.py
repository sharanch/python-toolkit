"""
alert.py — Shared alerting module
Supports Slack webhooks and SMTP email alerts.
"""

import json
import logging
import smtplib
from email.mime.text import MIMEText

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

logger = logging.getLogger(__name__)


def send_slack(webhook_url, message):
    """
    Send an alert to a Slack channel via webhook.

    Args:
        webhook_url (str): Slack incoming webhook URL
        message (str): Message to send

    Returns:
        bool: True if successful, False otherwise
    """
    if not REQUESTS_AVAILABLE:
        logger.error("requests library not installed. Run: pip install requests")
        return False

    if not webhook_url:
        logger.warning("Slack webhook URL not configured, skipping Slack alert")
        return False

    try:
        payload = {"text": message}
        response = requests.post(webhook_url, json=payload, timeout=5)
        if response.status_code == 200:
            logger.info("Slack alert sent successfully")
            return True
        else:
            logger.error(f"Slack alert failed with status {response.status_code}")
            return False
    except requests.exceptions.ConnectionError:
        logger.error("Could not reach Slack — connection error")
        return False
    except Exception as e:
        logger.error(f"Slack alert failed: {e}")
        return False


def send_email(config, subject, body):
    """
    Send an alert email via SMTP.

    Args:
        config (dict): alerts config block from config.json
        subject (str): Email subject
        body (str): Email body

    Returns:
        bool: True if successful, False otherwise
    """
    smtp_user = config.get("smtp_user")
    smtp_password = config.get("smtp_password")

    if not smtp_user or not smtp_password:
        logger.warning("SMTP credentials not configured, skipping email alert")
        return False

    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = config.get("alert_from", "alerts@company.com")
        msg["To"] = config.get("alert_to", "oncall@company.com")

        with smtplib.SMTP(config.get("smtp_host", "smtp.gmail.com"),
                          config.get("smtp_port", 587)) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)

        logger.info(f"Email alert sent to {msg['To']}")
        return True

    except smtplib.SMTPAuthenticationError:
        logger.error("SMTP authentication failed — check credentials")
        return False
    except smtplib.SMTPException as e:
        logger.error(f"SMTP error: {e}")
        return False
    except Exception as e:
        logger.error(f"Email alert failed: {e}")
        return False


def send_alert(config, subject, body):
    """
    Send alert via all configured channels (Slack + Email).

    Args:
        config (dict): Full config dict loaded from config.json
        subject (str): Alert subject/title
        body (str): Alert message body
    """
    alerts_config = config.get("alerts", {})
    slack_url = alerts_config.get("slack_webhook_url", "")

    slack_message = f"*{subject}*\n{body}"
    send_slack(slack_url, slack_message)
    send_email(alerts_config, subject, body)


# ---------------------------------------------------------------------------
# Aliases — alternate call signatures used by some scripts
# ---------------------------------------------------------------------------

def send_slack_alert(message, webhook_url):
    """Alias for send_slack with swapped argument order."""
    return send_slack(webhook_url, message)


def send_email_alert(subject, body, config):
    """Alias for send_email with positional subject/body."""
    return send_email(config, subject, body)