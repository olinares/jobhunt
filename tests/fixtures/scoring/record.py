"""Re-record the scorer fixtures against the real Messages API.

The fixtures in this folder started out synthetic (`"_synthetic": true`): hand-built in the
shape of real responses because no API key was available when the scorer was written.
Run this once with a key to replace each fixture's `response` with a real one:

    ANTHROPIC_API_KEY=... python tests/fixtures/scoring/record.py

It sends each fixture's `job` through the same request `score_job` builds, but with the
PLACEHOLDER resumes below, never real ones: real resume text must not reach the fixtures.
A fixture's optional `record_overrides` are applied on top of the request (the "truncated"
case sets a tiny `max_tokens` to get a real `max_tokens` stop). The saved file keeps the job
and the response only; the request (and so the resume text) is not stored.

Afterwards, check `expected` in each file still holds and run `pytest -q`.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import anthropic

from jobhunt.models import BoardRef, Job
from jobhunt.scoring import Resumes, build_request

HERE = Path(__file__).parent

# A made-up candidate. Not anyone's real resume.
PLACEHOLDER_RESUMES = Resumes(
    se=(
        "PLACEHOLDER RESUME (SE version) for a fictional candidate, used only to record test "
        "fixtures.\n"
        "Solutions Engineer, Example SaaS Co. Ran technical discovery and live demos for "
        "prospects alongside account executives; scoped and led proofs of concept in Python "
        "and SQL; answered security questionnaires.\n"
        "Support Engineer, Example Software Inc. Debugged customer REST API integrations."
    ),
    fde=(
        "PLACEHOLDER RESUME (FDE version) for a fictional candidate, used only to record test "
        "fixtures.\n"
        "Solutions Engineer, Example SaaS Co. Embedded with enterprise customers after the sale "
        "to build Python integrations and data pipelines on the platform, and owned onboarding "
        "for several accounts.\n"
        "Support Engineer, Example Software Inc. Wrote internal tools in Python; some "
        "TypeScript."
    ),
)


def job_from_fixture(data: dict) -> Job:
    fields = dict(data)
    fields["board"] = BoardRef(**fields["board"])
    if fields.get("posted_at"):
        fields["posted_at"] = datetime.fromisoformat(fields["posted_at"])
    return Job(**fields)


def main() -> int:
    client = anthropic.Anthropic()
    for path in sorted(HERE.glob("*.json")):
        doc = json.loads(path.read_text())
        job = job_from_fixture(doc["job"])
        request = build_request(job, PLACEHOLDER_RESUMES) | doc.get("record_overrides", {})
        message = client.messages.create(**request)
        doc.pop("_synthetic", None)
        doc.pop("_note", None)
        doc["response"] = message.to_dict()
        path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        usage = message.usage
        print(
            f"{path.name}: stop={message.stop_reason} input={usage.input_tokens} "
            f"output={usage.output_tokens} cache_read={usage.cache_read_input_tokens} "
            f"cache_write={usage.cache_creation_input_tokens}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
