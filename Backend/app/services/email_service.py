# app/services/email_service.py
"""
SMTP e-mail delivery for scheduled reports.

All SMTP credentials are read from environment variables (.env) — nothing is
hardcoded. The single test recipient defaults to REPORT_RECIPIENT_EMAIL.
"""

import os
import ssl
import smtplib
import logging
from email.message import EmailMessage

logger = logging.getLogger(__name__)

# Single pre-configured recipient for the current testing phase. Overridable via
# the REPORT_RECIPIENT_EMAIL environment variable. Multi-recipient support comes later.
DEFAULT_RECIPIENT_EMAIL = "ptshivaji8@gmail.com"


def get_recipient_email() -> str:
    """The configured destination address for scheduled / test report e-mails."""
    return os.getenv("REPORT_RECIPIENT_EMAIL", DEFAULT_RECIPIENT_EMAIL).strip()


def get_smtp_config() -> dict:
    """Read SMTP settings from environment variables."""
    username = os.getenv("SMTP_USERNAME", "").strip()
    return {
        "host":       os.getenv("SMTP_HOST", "").strip(),
        "port":       int(os.getenv("SMTP_PORT", "587") or "587"),
        "username":   username,
        "password":   os.getenv("SMTP_PASSWORD", "").strip(),
        "from_email": os.getenv("SMTP_FROM_EMAIL", username).strip(),
    }


# Environment variables required before e-mail sending can be enabled.
REQUIRED_VARS = ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD")


def get_missing_config() -> list[str]:
    """Return the names of required SMTP env vars that are not set."""
    return [v for v in REQUIRED_VARS if not os.getenv(v, "").strip()]


def is_configured() -> bool:
    return not get_missing_config()


def log_startup_status() -> bool:
    """Log SMTP readiness at application startup. Returns True if configured."""
    missing = get_missing_config()
    if missing:
        logger.warning(
            "SMTP is NOT configured - e-mail sending is DISABLED. "
            "Missing environment variable(s): %s. "
            "Set them in Backend/.env (see .env.example) to enable scheduled-report e-mails.",
            ", ".join(missing),
        )
        return False
    cfg = get_smtp_config()
    logger.info(
        "SMTP configured - e-mail sending ENABLED. host=%s port=%s from=%s",
        cfg["host"], cfg["port"], cfg["from_email"] or cfg["username"],
    )
    return True


def send_email_with_attachment(
    to_email: str,
    subject: str,
    body: str,
    attachment_bytes: bytes | None = None,
    attachment_filename: str | None = None,
    mime_main: str = "application",
    mime_sub: str = "octet-stream",
) -> dict:
    """
    Send an e-mail with an optional file attachment.

    Returns a structured result: {"success": bool, "error": str | None}.
    Never raises — failures are logged and returned so callers can report status.
    """
    cfg = get_smtp_config()

    missing = get_missing_config()
    if missing:
        err = "SMTP is not configured. Missing environment variable(s): " + ", ".join(missing)
        logger.error("Email send aborted -> %s", err)
        return {"success": False, "error": err}

    msg = EmailMessage()
    msg["From"] = cfg["from_email"] or cfg["username"]
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    if attachment_bytes is not None and attachment_filename:
        msg.add_attachment(
            attachment_bytes,
            maintype=mime_main,
            subtype=mime_sub,
            filename=attachment_filename,
        )

    logger.info(
        "Email send requested -> recipient=%s | subject=%s | attachment=%s | host=%s:%s",
        to_email, subject, attachment_filename or "none", cfg["host"], cfg["port"],
    )

    try:
        if cfg["port"] == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30, context=context) as server:
                logger.info("SMTP SSL connection established -> %s:%s", cfg["host"], cfg["port"])
                server.login(cfg["username"], cfg["password"])
                logger.info("SMTP authentication successful -> user=%s", cfg["username"])
                server.send_message(msg)
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                logger.info("SMTP TLS connection established -> %s:%s", cfg["host"], cfg["port"])
                server.login(cfg["username"], cfg["password"])
                logger.info("SMTP authentication successful -> user=%s", cfg["username"])
                server.send_message(msg)

        logger.info(
            "Email status -> SUCCESS | recipient=%s | subject=%s | attachment=%s",
            to_email, subject, attachment_filename or "none",
        )
        return {"success": True, "error": None}

    except Exception as e:  # noqa: BLE001 — report any SMTP/network failure to caller
        err = f"{type(e).__name__}: {e}"
        logger.error(
            "Email status -> FAILED | recipient=%s | subject=%s | attachment=%s | error=%s",
            to_email, subject, attachment_filename or "none", err, exc_info=True,
        )
        return {"success": False, "error": err}
