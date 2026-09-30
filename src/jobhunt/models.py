"""Shared contract for all agents. Do not change without asking (see CLAUDE.md)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

ATS = Literal["greenhouse", "lever", "ashby", "workday", "gem"]
Variant = Literal["se", "fde"]  # which resume fits: Solutions Engineer or Forward Deployed


@dataclass(frozen=True)
class BoardRef:
    """One company's job board on one ATS."""

    ats: ATS
    slug: str  # greenhouse/lever/ashby/gem board slug; for workday, the tenant
    host: str | None = None  # workday only, e.g. "nvidia.wd5.myworkdayjobs.com"
    site: str | None = None  # workday only, the career site name
    company_name: str | None = None

    def key(self) -> str:
        if self.ats == "workday":
            return f"workday:{self.host}/{self.site}"
        return f"{self.ats}:{self.slug}"


@dataclass
class Job:
    board: BoardRef
    external_id: str
    title: str
    company: str
    url: str
    locations: list[str] = field(default_factory=list)  # raw strings as the ATS gives them
    remote: bool | None = None  # None = ATS doesn't say
    description_html: str | None = None
    posted_at: datetime | None = None
    pay_min: float | None = None
    pay_max: float | None = None
    pay_currency: str | None = None
    pay_period: str | None = None  # "year" | "hour" | ...
    department: str | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def uid(self) -> str:
        return f"{self.board.key()}:{self.external_id}"


@dataclass(frozen=True)
class Score:
    """How well one job fits, and which resume to send."""

    value: int  # 0-100 fit
    variant: Variant
    reason: str  # at most two sentences, shown in the digest
    pay_suspect: bool = False  # placeholder pay such as $1-$2; set in code, not by the model


@dataclass
class ScoredJob:
    job: Job
    score: Score


class Adapter(Protocol):
    ats: ATS

    def fetch(self, board: BoardRef, *, with_descriptions: bool = False) -> list[Job]:
        """Return every open job on the board. Raise BoardNotFound if the board doesn't exist."""
        ...


class BoardNotFound(Exception):
    pass
