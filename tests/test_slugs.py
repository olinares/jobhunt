import pytest

from jobhunt.discovery.slugs import board_from_url
from jobhunt.models import BoardRef

# --- Greenhouse -------------------------------------------------------------

GREENHOUSE_CASES = [
    ("https://boards.greenhouse.io/stripe", BoardRef("greenhouse", "stripe")),
    ("http://boards.greenhouse.io/stripe", BoardRef("greenhouse", "stripe")),
    ("https://boards.greenhouse.io/stripe/", BoardRef("greenhouse", "stripe")),
    (
        "https://boards.greenhouse.io/stripe/jobs/6789012",
        BoardRef("greenhouse", "stripe"),
    ),
    (
        "https://job-boards.greenhouse.io/anthropic/jobs/1234567",
        BoardRef("greenhouse", "anthropic"),
    ),
    (
        "https://boards.greenhouse.io/embed/job_board?for=stripe",
        BoardRef("greenhouse", "stripe"),
    ),
    (
        "https://boards.greenhouse.io/embed/job_app?for=stripe&token=abc123",
        BoardRef("greenhouse", "stripe"),
    ),
    (
        "https://boards.greenhouse.io/embed/job_board/js?for=notion",
        BoardRef("greenhouse", "notion"),
    ),
    (
        "https://boards.greenhouse.io/Notion/jobs/999",
        BoardRef("greenhouse", "Notion"),  # case preserved
    ),
]

# --- Lever --------------------------------------------------------------

LEVER_CASES = [
    ("https://jobs.lever.co/netflix", BoardRef("lever", "netflix")),
    ("https://jobs.lever.co/netflix/", BoardRef("lever", "netflix")),
    (
        "https://jobs.lever.co/netflix/abcd1234-ef56-7890-ab12-cd34ef567890",
        BoardRef("lever", "netflix"),
    ),
    (
        "https://jobs.lever.co/netflix/abcd1234-ef56-7890-ab12-cd34ef567890/apply",
        BoardRef("lever", "netflix"),
    ),
    ("http://jobs.lever.co/figma", BoardRef("lever", "figma")),
    (
        "https://jobs.lever.co/figma?lever-source=LinkedIn",
        BoardRef("lever", "figma"),
    ),
]

# --- Ashby ----------------------------------------------------------------

ASHBY_CASES = [
    ("https://jobs.ashbyhq.com/ramp", BoardRef("ashby", "ramp")),
    ("https://jobs.ashbyhq.com/ramp/", BoardRef("ashby", "ramp")),
    (
        "https://jobs.ashbyhq.com/ramp/8f4a1e2b-3c4d-4e5f-9a0b-1c2d3e4f5678",
        BoardRef("ashby", "ramp"),
    ),
    ("http://jobs.ashbyhq.com/linear", BoardRef("ashby", "linear")),
    (
        "https://jobs.ashbyhq.com/linear/8f4a1e2b-3c4d-4e5f-9a0b-1c2d3e4f5678?utm_source=x",
        BoardRef("ashby", "linear"),
    ),
]

# --- Gem --------------------------------------------------------------------

GEM_CASES = [
    ("https://jobs.gem.com/acme", BoardRef("gem", "acme")),
    ("https://jobs.gem.com/acme/", BoardRef("gem", "acme")),
    ("https://jobs.gem.com/acme/senior-solutions-engineer", BoardRef("gem", "acme")),
    ("http://jobs.gem.com/acme", BoardRef("gem", "acme")),
]

# --- Workday ------------------------------------------------------------

WORKDAY_CASES = [
    (
        (
            "https://nvidia.wd5.myworkdayjobs.com/External/job/US-CA-Santa-Clara/"
            "Solutions-Architect_JR1234567"
        ),
        BoardRef("workday", "nvidia", host="nvidia.wd5.myworkdayjobs.com", site="External"),
    ),
    (
        (
            "https://nvidia.wd5.myworkdayjobs.com/en-US/External/job/US-CA-Santa-Clara/"
            "Solutions-Architect_JR1234567"
        ),
        BoardRef("workday", "nvidia", host="nvidia.wd5.myworkdayjobs.com", site="External"),
    ),
    (
        (
            "http://salesforce.wd1.myworkdayjobs.com/External_Career_Site/job/"
            "California---San-Francisco/Forward-Deployed-Engineer_JR987654"
        ),
        BoardRef(
            "workday",
            "salesforce",
            host="salesforce.wd1.myworkdayjobs.com",
            site="External_Career_Site",
        ),
    ),
    (
        (
            "https://salesforce.wd1.myworkdayjobs.com/fr-FR/External_Career_Site/job/"
            "California---San-Francisco/Forward-Deployed-Engineer_JR987654"
        ),
        BoardRef(
            "workday",
            "salesforce",
            host="salesforce.wd1.myworkdayjobs.com",
            site="External_Career_Site",
        ),
    ),
    (
        (
            "https://databricks.wd12.myworkdayjobs.com/pt-BR/Databricks/job/"
            "Remote-United-States/Solutions-Engineer_JR2222"
        ),
        BoardRef(
            "workday", "databricks", host="databricks.wd12.myworkdayjobs.com", site="Databricks"
        ),
    ),
    (
        "https://databricks.wd12.myworkdayjobs.com/Databricks",
        BoardRef(
            "workday", "databricks", host="databricks.wd12.myworkdayjobs.com", site="Databricks"
        ),
    ),
]

# --- Non-board URLs -> None -----------------------------------------------

NON_BOARD_CASES = [
    "https://www.greenhouse.io/",
    "https://www.greenhouse.io/pricing",
    "https://boards.greenhouse.io/",
    "https://www.lever.co/",
    "https://www.lever.co/pricing",
    "https://jobs.lever.co/",
    "https://jobs.ashbyhq.com/",
    "https://acme.com/careers",
    "https://acme.com/careers/solutions-engineer",
    "https://jobs.gem.com/",
    "https://nvidia.wd5.myworkdayjobs.com/",
    "https://myworkdayjobs.com/",
    "not-a-url",
    "",
]

ALL_BOARD_CASES = GREENHOUSE_CASES + LEVER_CASES + ASHBY_CASES + GEM_CASES + WORKDAY_CASES


@pytest.mark.parametrize("url,expected", ALL_BOARD_CASES)
def test_board_from_url_recognizes_board(url: str, expected: BoardRef) -> None:
    assert board_from_url(url) == expected


@pytest.mark.parametrize("url", NON_BOARD_CASES)
def test_board_from_url_ignores_non_board(url: str) -> None:
    assert board_from_url(url) is None


def test_total_board_case_count_is_at_least_30() -> None:
    assert len(ALL_BOARD_CASES) >= 30
