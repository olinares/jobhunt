"""The public pages behind the digest's Approve/Skip links.

``GET /a/{token}`` only shows a confirmation page; ``POST /a/{token}`` changes the status.
Mail scanners and link previewers fetch URLs with GET, so they can never approve anything.
The POST is a compare-and-set from ``new``, so it never overwrites a status Oz set some
other way (``applied``, ``interviewing``, ...).

`link_routes` returns plain route definitions; brief P registers them on the FastMCP server
with ``custom_route``, so no app is built here. The token is the only credential: these
routes sit outside the MCP OAuth, and the page shows nothing a stranger holding a leaked
link shouldn't see (title, company, status; never the score reason, which a model wrote
after reading the resume).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from html import escape
from typing import NamedTuple

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from jobhunt.links import LinkClaim, LinkError, secret_bytes, verify
from jobhunt.store import Store

StoreFactory = Callable[[], Store]
Endpoint = Callable[[Request], Awaitable[Response]]

TARGET_STATUS = {"approve": "approved", "skip": "skipped"}
_VERB = {"approve": "Approve", "skip": "Skip"}

HEADERS = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
    ),
}


class LinkRoute(NamedTuple):
    """What ``FastMCP.custom_route(path, methods)`` needs, plus the handler."""

    path: str
    methods: list[str]
    endpoint: Endpoint


def link_routes(store_factory: StoreFactory, *, secret: str | bytes) -> list[LinkRoute]:
    """The ``/a/{token}`` routes. `store_factory` opens a store per request (e.g.
    ``lambda: open_store(url)``); it's used as a context manager, so the store is closed
    after each request."""
    key = secret_bytes(secret)  # refuse a short secret at startup, not on the first click

    async def confirm(request: Request) -> Response:
        claim = _claim(request, key)
        if isinstance(claim, Response):
            return claim
        record = await run_in_threadpool(_read, store_factory, claim.uid)
        if record is None:
            return _unknown()
        title, company, status = record
        verb = _VERB[claim.action]
        target = TARGET_STATUS[claim.action]
        if status == "new":
            action = (
                f'<form method="post" action="{escape(request.url.path, quote=True)}">'
                f'<button type="submit" style="{_BUTTON}">{verb}</button></form>'
            )
        elif status == target:
            action = f"<p>Already {escape(target)}.</p>"
        else:
            action = f"<p>Status is {escape(status)}; this link won't change it.</p>"
        body = (
            f"<h1>{verb} this job?</h1>"
            f"<p><strong>{escape(title)}</strong><br>{escape(company)}</p>"
            f"<p>Current status: {escape(status)}</p>{action}"
        )
        return _page(f"{verb}: {title}", body)

    async def apply(request: Request) -> Response:
        claim = _claim(request, key)
        if isinstance(claim, Response):
            return claim
        target = TARGET_STATUS[claim.action]
        outcome = await run_in_threadpool(_write, store_factory, claim.uid, target)
        if outcome is None:
            return _unknown()
        changed, status = outcome
        if changed:
            message = f"{target.capitalize()} ✓"
        elif status == target:
            message = f"Already {target}"
        else:
            message = f"Status is {status}; not changed"
        return _page(message, f"<h1>{escape(message)}</h1>")

    path = "/a/{token}"
    return [LinkRoute(path, ["GET"], confirm), LinkRoute(path, ["POST"], apply)]


# -- store access (sync; run in a worker thread) ---------------------------------


def _read(store_factory: StoreFactory, uid: str) -> tuple[str, str, str] | None:
    with store_factory() as store:
        record = store.get_job(uid)
    if record is None:
        return None
    return record.job.title, record.job.company, record.status


def _write(store_factory: StoreFactory, uid: str, target: str) -> tuple[bool, str] | None:
    """``(changed, status_now)``, or ``None`` for an unknown uid."""
    with store_factory() as store:
        if store.set_status_if(uid, target, expected="new"):
            return True, target
        record = store.get_job(uid)
    return None if record is None else (False, record.status)


# -- responses -------------------------------------------------------------------


def _claim(request: Request, key: bytes) -> LinkClaim | Response:
    try:
        return verify(request.path_params["token"], secret=key)
    except LinkError as exc:
        return _page("Link not valid", f"<h1>Link not valid</h1><p>{escape(str(exc))}.</p>", 400)


def _unknown() -> Response:
    return _page("Job not found", "<h1>Job not found</h1>", 404)


_BUTTON = "font-size:18px;padding:10px 24px;border-radius:6px;border:1px solid #444;"


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    html = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{escape(title)} · jobhunt</title></head>"
        '<body style="font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:480px;'
        'margin:40px auto;padding:0 16px;color:#222;font-size:17px;line-height:1.4">'
        f"{body}</body></html>"
    )
    return HTMLResponse(html, status_code=status_code, headers=HEADERS)
