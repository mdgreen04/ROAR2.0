"""Email transport. SMTP (Gmail app password) or write an .eml file for inspection."""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

log = logging.getLogger("roar.send")


def build_message(*, subject: str, html_body: str, text_body: str, sender: str, sender_name: str,
                  recipient: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((sender_name, sender))
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="roar.local")
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg


def send_smtp(msg: EmailMessage, host: str, port: int, user: str, password: str) -> None:
    with smtplib.SMTP(host, port, timeout=60) as s:
        s.ehlo()
        s.starttls()
        s.ehlo()
        s.login(user, password)
        s.send_message(msg)
    log.info("sent %r to %s via %s", msg["Subject"], msg["To"], host)


def deliver(cfg: dict, *, subject: str, html_body: str, text_body: str, out_dir: Path,
            transport: str | None = None, recipient: str | None = None) -> Path | None:
    """Send (smtp) or save (file). Returns the .eml path when written."""
    e_cfg = cfg["settings"]["email"]
    d_cfg = cfg["settings"]["digest"]
    transport = transport or e_cfg.get("transport", "file")
    recipient = recipient or os.environ.get("ROAR_TO") or d_cfg["recipient"]
    if recipient.endswith("@example.com"):
        raise SystemExit("no recipient configured: set ROAR_TO (env / .env / GitHub secret) or --to")
    user = os.environ.get("ROAR_SMTP_USER", "")
    sender = os.environ.get("ROAR_FROM", user or recipient)
    msg = build_message(subject=subject, html_body=html_body, text_body=text_body, sender=sender,
                        sender_name=d_cfg.get("from_name", "ROAR 2.0"), recipient=recipient)
    if transport == "smtp":
        password = os.environ.get("ROAR_SMTP_PASSWORD", "")
        if not user or not password:
            raise SystemExit("ROAR_SMTP_USER / ROAR_SMTP_PASSWORD are not set (use a Gmail app password)")
        send_smtp(msg, e_cfg.get("smtp_host", "smtp.gmail.com"), int(e_cfg.get("smtp_port", 587)), user, password)
        return None
    if transport == "file":
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / "digest.eml"
        p.write_bytes(bytes(msg))
        log.info("email written to %s (transport=file)", p)
        return p
    raise SystemExit(f"unknown email transport: {transport}")
