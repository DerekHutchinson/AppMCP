"""Server-side email sending for the app email endpoint (SendGrid v3).

Apps never hold the SendGrid key or choose the From address; they post a
recipient/subject/body to /a/{slug}/email and this module sends it. Guardrails
enforced here (defense-in-depth alongside the endpoint):

  * recipients are restricted to the org domain (no open relay to outsiders)
  * a per-app per-minute cap guards against runaway agent loops
  * the From address is fixed from config

Uses httpx directly against the SendGrid v3 mail/send API (no extra SDK dep).
"""
import time
from collections import defaultdict, deque

import httpx

from config import settings

SENDGRID_SEND_URL = "https://api.sendgrid.com/v3/mail/send"

# Per-app send timestamps for the sliding-window rate limit (per process).
_SEND_LOG: dict[str, deque] = defaultdict(deque)


class EmailError(Exception):
    """Raised when an email request is invalid or sending fails."""


def _normalize_recipients(to) -> list[str]:
    """Accept a string (comma/semicolon/space separated) or a list; return emails."""
    if isinstance(to, str):
        parts = [p.strip() for p in to.replace(";", ",").replace(" ", ",").split(",")]
    elif isinstance(to, (list, tuple)):
        parts = [str(p).strip() for p in to]
    else:
        raise EmailError("'to' must be a string or a list of email addresses.")
    return [p for p in parts if p]


def _validate_domain(recipients: list[str]) -> None:
    domain = settings.allowed_email_domain.strip().lower()
    bad = [r for r in recipients if not r.lower().endswith(domain)]
    if bad:
        raise EmailError(
            f"Recipients must be internal ({domain}); rejected: {', '.join(bad)}"
        )


def _check_rate(slug: str) -> None:
    limit = settings.email_rate_per_min
    if limit <= 0:
        return
    now = time.monotonic()
    log = _SEND_LOG[slug]
    while log and now - log[0] > 60:
        log.popleft()
    if len(log) >= limit:
        raise EmailError("Email rate limit exceeded for this app; try again shortly.")
    log.append(now)


async def send_app_email(
    slug: str,
    sender: str,
    to,
    subject: str,
    html: str | None = None,
    text: str | None = None,
) -> dict:
    """Validate and send an internal email on behalf of an app.

    Returns {"sent": N, "recipients": [...]}. Raises EmailError on any problem.
    """
    if not settings.email_configured:
        raise EmailError("Email is not configured on this server.")

    recipients = _normalize_recipients(to)
    if not recipients:
        raise EmailError("At least one recipient is required.")
    if len(recipients) > settings.email_max_recipients:
        raise EmailError(
            f"Too many recipients (max {settings.email_max_recipients})."
        )
    _validate_domain(recipients)

    subject = (subject or "").strip()
    if not subject:
        raise EmailError("A non-empty subject is required.")

    html = (html or "").strip()
    text = (text or "").strip()
    if not html and not text:
        raise EmailError("Provide an html or text body.")

    _check_rate(slug)

    content = []
    if text:
        content.append({"type": "text/plain", "value": text})
    if html:
        content.append({"type": "text/html", "value": html})

    from_obj = {"email": settings.email_from}
    if settings.email_from_name.strip():
        from_obj["name"] = settings.email_from_name.strip()

    payload = {
        "personalizations": [{"to": [{"email": r} for r in recipients]}],
        "from": from_obj,
        "subject": subject,
        "content": content,
    }
    # Replies go to the person who triggered the send.
    if sender:
        payload["reply_to"] = {"email": sender}

    headers = {
        "Authorization": f"Bearer {settings.sendgrid_api_key.strip()}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(SENDGRID_SEND_URL, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise EmailError(f"Could not reach the email provider: {exc}") from exc

    if resp.status_code >= 400:
        raise EmailError(
            f"Email provider rejected the send ({resp.status_code}): "
            f"{resp.text[:300]}"
        )

    return {"sent": len(recipients), "recipients": recipients}
