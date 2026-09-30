"""Short human-readable renderings of a job's location and pay, shared by the CLI and digest."""

from __future__ import annotations

from jobhunt.models import Job

MAX_LOCATIONS_SHOWN = 3


def format_locations(job: Job, *, max_shown: int = MAX_LOCATIONS_SHOWN) -> str | None:
    """`"A; B; C (+2 more)"`, `"Remote"` when only the remote flag is known, else None."""
    if job.locations:
        shown = job.locations[:max_shown]
        extra = len(job.locations) - len(shown)
        return "; ".join(shown) + (f" (+{extra} more)" if extra else "")
    if job.remote:
        return "Remote"
    return None


def format_pay(job: Job) -> str | None:
    """`"USD 200,000–250,000/year"`; a single amount when min equals max; None if no pay."""
    if job.pay_min is None and job.pay_max is None:
        return None
    amounts = [f"{v:,.0f}" for v in (job.pay_min, job.pay_max) if v is not None]
    text = "–".join(dict.fromkeys(amounts))
    if job.pay_currency:
        text = f"{job.pay_currency} {text}"
    if job.pay_period:
        text += f"/{job.pay_period}"
    return text
