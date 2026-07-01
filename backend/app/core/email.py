"""
Email delivery for login OTPs.

- Production: when SMTP_HOST is configured, the OTP is sent via SMTP.
- Local dev: when SMTP is not configured, the OTP is logged to the server
  console (and the API also returns it to the client) so the flow can be
  tested end-to-end without an email server.
"""
import asyncio
import smtplib
import logging
from email.message import EmailMessage
from app.core.config import settings

logger = logging.getLogger("clari5pay.email")


def _otp_email_html(otp: str, minutes: int) -> str:
    """Branded verification-code email. The OTP and validity window are injected dynamically."""
    return f"""<!DOCTYPE html><html> <head> <meta charset="UTF-8"> <title>Verification Code</title> </head> <body style="margin:0; padding:0; background-color:#f5f6f8; font-family:Arial, sans-serif;"> <table width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f5f6f8"> <tr> <td align="center"> <!-- Main Container --> <table width="420" cellpadding="0" cellspacing="0" border="0" style="background:#ffffff; margin:40px auto; border-radius:10px;"> <tr> <td style="padding:30px; text-align:center;"> <!-- Title --> <p style="font-size:20px; font-weight:600; color:#333333; margin-bottom:25px;"> Here is your verification code: </p> <!-- Code Box --> <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid #e0e0e0; border-radius:10px; background:#fafafa;"> <tr> <td style="padding:25px; text-align:center;"> <span style="font-size:30px; font-weight:bold; letter-spacing:6px; color:#6a5acd;"> {otp} </span> </td> </tr> </table> <!-- Info --> <p style="font-size:14px; color:#555555; margin-top:20px;"> Please make sure you never share this code with anyone. </p> <!-- Note --> <p style="font-size:14px; color:#333333; margin-top:10px;"> <strong>Note:</strong> The code will expire in {minutes} minutes. </p> </td> </tr> </table> </td> </tr> </table></body> </html>"""


def _send_sync(to: str, subject: str, body: str, html: str | None = None) -> None:
    msg = EmailMessage()
    msg["From"] = settings.SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as server:
        if settings.SMTP_TLS:
            server.starttls()
        if settings.SMTP_USER:
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.send_message(msg)


async def send_otp_email(to: str, otp: str, name: str = "", purpose: str = "login") -> bool:
    """Send the OTP using the branded template. Returns True if a real email was dispatched,
    False in dev (logged only). The same template is used for login and password-reset codes."""
    minutes = settings.OTP_EXPIRE_MINUTES
    subject = "[DEMO] Your Verification Code" if settings.is_demo else "Your Verification Code"
    action = "reset your password" if purpose == "reset" else "sign in"
    body = (
        f"Hello {name or ''},\n\n"
        f"Here is your verification code: {otp}\n"
        f"Please make sure you never share this code with anyone.\n"
        f"Note: The code will expire in {minutes} minutes.\n\n"
        f"If you did not try to {action}, please ignore this email.\n"
    )
    html = _otp_email_html(otp, minutes)
    if not settings.email_configured:
        logger.warning("[DEV OTP] %s -> %s (purpose=%s; no SMTP configured; not emailed)", to, otp, purpose)
        print(f"[DEV OTP] {purpose} code for {to}: {otp}", flush=True)
        return False
    try:
        await asyncio.to_thread(_send_sync, to, subject, body, html)
        return True
    except Exception as exc:  # pragma: no cover - network/SMTP errors
        logger.error("Failed to send OTP email to %s: %s", to, exc)
        # Fall back to logging so the flow isn't blocked if SMTP fails.
        print(f"[OTP FALLBACK] {purpose} code for {to}: {otp}", flush=True)
        return False


def mask_email(email: str) -> str:
    """Mask an email for display, e.g. 'nikhila@gmail.com' -> 'ni****a@gmail.com'."""
    try:
        local, domain = email.split("@", 1)
    except ValueError:
        return email
    if len(local) <= 2:
        masked = local[0] + "*"
    else:
        masked = local[:2] + "*" * max(1, len(local) - 3) + local[-1]
    return f"{masked}@{domain}"
