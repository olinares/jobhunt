"""Render the daily digest and send it over Gmail SMTP. Standard library only."""

from __future__ import annotations

import os
import smtplib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from html import escape

from jobhunt.formatting import format_locations as _format_locations
from jobhunt.formatting import format_pay as _format_pay
from jobhunt.models import ScoredJob

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
VARIANT_LABEL = {"se": "SE", "fde": "FDE"}


@dataclass
class DigestStats:
    """Pipeline health numbers for the footer, so the email says whether the run was healthy."""

    boards_polled: int = 0
    jobs_fetched: int = 0
    relevant: int = 0
    new: int = 0
    closed: int = 0
    scored: int = 0
    score_failures: int = 0
    boards_failed: int = 0


class DigestConfigError(RuntimeError):
    """A required environment variable is missing."""


def render_digest(items: list[ScoredJob], *, day: date, stats: DigestStats) -> tuple[str, str, str]:
    """Return (subject, text, html). Items are already ranked; they are numbered 1..N."""
    subject = _subject(items, day)
    return subject, _render_text(items, day, stats), _render_html(items, day, stats)


def send_email(
    subject: str,
    text: str,
    html: str,
    *,
    smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP_SSL,
) -> None:
    address = _require_env("GMAIL_ADDRESS")
    password = _require_env("GMAIL_APP_PASSWORD")
    to = os.environ.get("DIGEST_TO") or address

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = address
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    with smtp_factory(SMTP_HOST, SMTP_PORT) as server:
        server.login(address, password)
        server.send_message(msg)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise DigestConfigError(f"environment variable {name} is not set")
    return value


def _when(day: date) -> str:
    return f"{day:%a %b} {day.day}"


def _subject(items: list[ScoredJob], day: date) -> str:
    if not items:
        return f"jobhunt · {_when(day)} · no new jobs"
    top = max(i.score.value for i in items)
    return f"jobhunt · {_when(day)} · {len(items)} new (top {top})"


def _pay_line(item: ScoredJob) -> str | None:
    pay = _format_pay(item.job)
    if pay and item.score.pay_suspect:
        return f"⚠ {pay} (looks like a placeholder)"
    return pay


def _variant(item: ScoredJob) -> str:
    return VARIANT_LABEL.get(item.score.variant, item.score.variant.upper())


def _footer_lines(stats: DigestStats) -> list[str]:
    summary = (
        f"{stats.boards_polled} boards polled · {stats.jobs_fetched} jobs fetched · "
        f"{stats.relevant} relevant · {stats.new} new · {stats.closed} closed · "
        f"{stats.scored} scored"
    )
    lines = [summary]
    if stats.boards_failed:
        lines.append(f"⚠ {stats.boards_failed} board(s) failed to load")
    if stats.score_failures:
        lines.append(f"⚠ {stats.score_failures} job(s) failed scoring")
    return lines


def _render_text(items: list[ScoredJob], day: date, stats: DigestStats) -> str:
    lines = [f"jobhunt digest · {_when(day)}", ""]
    if not items:
        lines += ["No new jobs today.", ""]
    for n, item in enumerate(items, start=1):
        job = item.job
        parts = [f"#{n}", str(item.score.value), _variant(item), job.title, job.company]
        if loc := _format_locations(job):
            parts.append(loc)
        if pay := _pay_line(item):
            parts.append(pay)
        lines.append(" · ".join(parts))
        lines.append(f"    {item.score.reason}")
        lines.append(f"    {job.url}")
        lines.append("")
    lines.append("--")
    lines += _footer_lines(stats)
    return "\n".join(lines) + "\n"


def _render_html(items: list[ScoredJob], day: date, stats: DigestStats) -> str:
    e = escape
    body = [
        (
            '<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;'
            'max-width:640px;margin:0 auto;padding:12px;color:#222;font-size:16px;line-height:1.4">'
        ),
        f'<h2 style="margin:0 0 12px;font-size:20px">jobhunt digest · {e(_when(day))}</h2>',
    ]
    if not items:
        body.append('<p style="margin:12px 0">No new jobs today.</p>')
    for n, item in enumerate(items, start=1):
        job = item.job
        meta = [e(job.company)]
        if loc := _format_locations(job):
            meta.append(e(loc))
        if pay := _pay_line(item):
            meta.append(e(pay))
        body += [
            '<div style="margin:0 0 16px;padding:0 0 12px;border-bottom:1px solid #ddd">',
            (
                f'<div style="font-weight:600">#{n} · {item.score.value} · {e(_variant(item))}'
                f" · {e(job.title)}</div>"
            ),
            f'<div style="color:#555;font-size:14px">{" · ".join(meta)}</div>',
            f'<div style="margin:4px 0">{e(item.score.reason)}</div>',
            f'<div><a href="{e(job.url, quote=True)}">{e(job.url)}</a></div>',
            "</div>",
        ]
    footer = "<br>".join(e(line) for line in _footer_lines(stats))
    body.append(f'<p style="color:#777;font-size:13px;margin:12px 0 0">{footer}</p>')
    body.append("</div>")
    return "\n".join(body) + "\n"
