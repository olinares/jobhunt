import httpx
import pytest
import respx

from jobhunt.discovery.queries import DEFAULT_DOMAINS, build_queries
from jobhunt.discovery.search import (
    SearchConfigError,
    SerperClient,
    run_capped_search,
)

# --- build_queries -----------------------------------------------------------


def test_build_queries_one_per_domain_title_region() -> None:
    queries = build_queries(
        titles=["Solutions Engineer"],
        region_terms=["San Francisco"],
        domains=("jobs.ashbyhq.com",),
    )
    assert queries == ['site:jobs.ashbyhq.com "Solutions Engineer" "San Francisco"']


def test_build_queries_cross_product_size() -> None:
    queries = build_queries(
        titles=["Solutions Engineer", "Forward Deployed Engineer"],
        region_terms=["San Francisco", "Remote"],
        domains=("jobs.ashbyhq.com", "jobs.lever.co"),
    )
    assert len(queries) == 2 * 2 * 2


def test_build_queries_quotes_title_and_region_as_phrases() -> None:
    [query] = build_queries(
        titles=["Forward Deployed Engineer"],
        region_terms=["New York"],
        domains=("jobs.gem.com",),
    )
    assert '"Forward Deployed Engineer"' in query
    assert '"New York"' in query
    assert query.startswith("site:jobs.gem.com ")


def test_build_queries_no_regions_omits_region_term() -> None:
    queries = build_queries(
        titles=["Solutions Engineer"],
        region_terms=[],
        domains=("jobs.lever.co",),
    )
    assert queries == ['site:jobs.lever.co "Solutions Engineer"']


def test_build_queries_uses_default_domains_when_unspecified() -> None:
    queries = build_queries(titles=["Solutions Engineer"], region_terms=["Remote"])
    assert len(queries) == len(DEFAULT_DOMAINS)
    for domain in DEFAULT_DOMAINS:
        assert any(q.startswith(f"site:{domain} ") for q in queries)


def test_default_domains_cover_all_required_ats() -> None:
    required = {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "jobs.lever.co",
        "jobs.ashbyhq.com",
        "myworkdayjobs.com",
        "jobs.gem.com",
    }
    assert required.issubset(set(DEFAULT_DOMAINS))


# --- SerperClient / search client -------------------------------------------


def test_serper_client_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEARCH_API_KEY", raising=False)
    with pytest.raises(SearchConfigError):
        SerperClient()


def test_serper_client_reads_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_API_KEY", "env-key")
    client = SerperClient()
    assert client._api_key == "env-key"
    client.close()


@respx.mock
def test_serper_client_search_returns_urls() -> None:
    route = respx.post("https://google.serper.dev/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "organic": [
                    {"link": "https://jobs.lever.co/acme"},
                    {"link": "https://jobs.ashbyhq.com/beta"},
                ]
            },
        )
    )
    client = SerperClient(api_key="test-key")
    urls = client.search('site:jobs.lever.co "Solutions Engineer"', count=10)
    client.close()

    assert route.called
    request = route.calls.last.request
    assert request.headers["X-API-KEY"] == "test-key"
    assert "jobhunt/0.1" in request.headers["User-Agent"]
    assert urls == ["https://jobs.lever.co/acme", "https://jobs.ashbyhq.com/beta"]


@respx.mock
def test_serper_client_search_respects_count_cap() -> None:
    respx.post("https://google.serper.dev/search").mock(
        return_value=httpx.Response(
            200,
            json={"organic": [{"link": f"https://jobs.lever.co/co{i}"} for i in range(5)]},
        )
    )
    client = SerperClient(api_key="test-key")
    urls = client.search("query", count=3)
    client.close()
    assert len(urls) == 3


@respx.mock
def test_serper_client_uses_injected_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"organic": [{"link": "https://jobs.gem.com/x"}]})

    transport = httpx.MockTransport(handler)
    client = SerperClient(api_key="test-key", transport=transport)
    urls = client.search("query")
    client.close()
    assert urls == ["https://jobs.gem.com/x"]


class _FakeSearchClient:
    """Minimal SearchClient for exercising run_capped_search without any transport."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def search(self, query: str, *, count: int = 10) -> list[str]:
        self.calls.append(query)
        return [f"https://jobs.lever.co/result-for-{len(self.calls)}"]


def test_run_capped_search_stops_at_max_queries() -> None:
    client = _FakeSearchClient()
    queries = [f"query-{i}" for i in range(10)]

    report = run_capped_search(client, queries, max_queries=3)

    assert report.queries_run == 3
    assert report.queries_skipped == 7
    assert report.capped is True
    assert len(client.calls) == 3
    assert len(report.urls) == 3


def test_run_capped_search_runs_all_when_under_cap() -> None:
    client = _FakeSearchClient()
    queries = ["a", "b"]

    report = run_capped_search(client, queries, max_queries=10)

    assert report.queries_run == 2
    assert report.queries_skipped == 0
    assert report.capped is False
