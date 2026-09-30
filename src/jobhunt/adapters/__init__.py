"""ATS adapters, plus a registry that builds one of each around a shared client."""

from __future__ import annotations

from jobhunt.adapters.ashby import AshbyAdapter
from jobhunt.adapters.gem import GemAdapter
from jobhunt.adapters.greenhouse import GreenhouseAdapter
from jobhunt.adapters.http import PoliteClient
from jobhunt.adapters.lever import LeverAdapter
from jobhunt.adapters.workday import WorkdayAdapter
from jobhunt.models import ATS, Adapter

ADAPTERS: dict[ATS, type] = {
    "greenhouse": GreenhouseAdapter,
    "lever": LeverAdapter,
    "ashby": AshbyAdapter,
    "workday": WorkdayAdapter,
    "gem": GemAdapter,
}


def build_adapters(client: PoliteClient) -> dict[ATS, Adapter]:
    """One adapter per ATS, all sharing `client` so per-host politeness holds across boards."""
    return {ats: cls(client) for ats, cls in ADAPTERS.items()}
