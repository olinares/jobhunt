"""Eval tooling tests: fake Anthropic client, SQLite store in tmp_path, no network."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml

from jobhunt.evals import (
    Label,
    Result,
    export,
    load_jobs,
    load_labels,
    percentile,
    pick_sample,
    render_report,
    run,
    run_eval,
    spearman,
    verdict_for,
)
from jobhunt.models import BoardRef, Job, Score
from jobhunt.scoring import Resumes
from jobhunt.store import open_store

RESUMES = Resumes(se="SE-RESUME-SENTINEL", fde="FDE-RESUME-SENTINEL")
REASON = "SECRET-REASON-TEXT you match on demos"


def make_job(i: int) -> Job:
    return Job(
        board=BoardRef("greenhouse", "acme"),
        external_id=str(i),
        title=f"Solutions Engineer {i}",
        company="Acme",
        url=f"https://example.com/{i}",
        locations=["Remote - US"],
        remote=True,
        description_html=f"<p>Build demos for customers. R&amp;D {i} &lt;fun&gt;</p>",
        pay_min=100000,
        pay_max=150000,
        pay_currency="USD",
        pay_period="year",
    )


def seed_store(path, count: int = 30):
    with open_store(str(path)) as store:
        store.upsert_jobs([make_job(i) for i in range(count)])
        for i in range(count):
            store.save_score(
                f"greenhouse:acme:{i}",
                Score(value=(i * 7) % 100, variant="se" if i % 2 else "fde", reason=REASON),
            )


class FakeMessages:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        v = self.values.pop(0)
        if isinstance(v, Exception):
            raise v
        value, variant = v
        text = json.dumps({"value": value, "variant": variant, "reason": REASON})
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(
                input_tokens=1000,
                output_tokens=50,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )


class FakeClient:
    def __init__(self, values):
        self.messages = FakeMessages(values)


# --------------------------------------------------------------------------- buckets


@pytest.mark.parametrize(
    ("score", "verdict"),
    [(100, "strong"), (70, "strong"), (69, "maybe"), (45, "maybe"), (44, "no"), (0, "no")],
)
def test_verdict_buckets_at_boundaries(score, verdict):
    assert verdict_for(score) == verdict


# --------------------------------------------------------------------------- spearman


def test_spearman_hand_computed():
    # ranks x: 1,2,3,4,5; ranks y: 2,1,4,3,5 -> d^2 = 1+1+1+1+0 = 4
    # rho = 1 - 6*4 / (5*24) = 0.8
    assert spearman([10, 20, 30, 40, 50], [2, 1, 4, 3, 5]) == pytest.approx(0.8)


def test_spearman_ties_and_degenerate():
    assert spearman([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)
    assert spearman([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
    # x = 1,1,2,3 -> ranks 1.5,1.5,3,4; y = 1,2,3,4. Pearson on ranks.
    assert spearman([1, 1, 2, 3], [1, 2, 3, 4]) == pytest.approx(0.9486832980505138)
    assert spearman([5, 5, 5], [1, 2, 3]) is None
    assert spearman([1], [1]) is None


def test_percentile_nearest_rank():
    values = list(range(1, 11))
    assert percentile(values, 50) == 5
    assert percentile(values, 95) == 10
    assert percentile([], 50) is None


# --------------------------------------------------------------------------- export


def test_export_is_deterministic_and_leaks_nothing(tmp_path):
    db = tmp_path / "jobs.db"
    seed_store(db)
    a, b = tmp_path / "a", tmp_path / "b"
    assert export(str(db), 12, a) == 12
    assert export(str(db), 12, b) == 12
    assert (a / "jobs.jsonl").read_text() == (b / "jobs.jsonl").read_text()
    assert (a / "labels.yaml").read_text() == (b / "labels.yaml").read_text()

    rows = [json.loads(line) for line in (a / "jobs.jsonl").read_text().splitlines()]
    assert len(rows) == 12
    assert set(rows[0]) == {
        "uid",
        "title",
        "company",
        "url",
        "locations",
        "remote",
        "pay",
        "description",
    }
    assert rows[0]["description"].startswith("Build demos for customers. R&D")
    blob = (a / "jobs.jsonl").read_text() + (a / "labels.yaml").read_text()
    assert "SECRET-REASON-TEXT" not in blob
    assert "reason" not in blob and "score" not in blob and 'variant": "se' not in blob

    labels = (a / "labels.yaml").read_text()
    assert labels.startswith("# This file is public.")
    entries = yaml.safe_load(labels)
    assert set(entries[0]) == {"uid", "title", "company", "verdict", "variant", "note"}
    assert all(e["verdict"] == "" for e in entries)


def test_pick_sample_is_stratified_and_mixes_variants():
    cands = [
        (f"u{i}", s, v)
        for i, (s, v) in enumerate(
            [(90, "se"), (80, "fde")] * 10
            + [(55, "se"), (60, "fde")] * 10
            + [(10, "se"), (20, "fde")] * 10
        )
    ]
    picked = pick_sample(cands, 12)
    by_uid = {u: (s, v) for u, s, v in cands}
    assert len(picked) == 12
    buckets = [verdict_for(by_uid[u][0]) for u in picked]
    assert [buckets.count(b) for b in ("strong", "maybe", "no")] == [4, 4, 4]
    assert {by_uid[u][1] for u in picked} == {"se", "fde"}
    assert pick_sample(cands, 12) == picked


def test_pick_sample_redistributes_when_a_bucket_is_short():
    cands = [("a", 90, "se"), ("b", 50, "fde")] + [(f"n{i}", 5, "se") for i in range(10)]
    assert len(pick_sample(cands, 9)) == 9
    assert len(pick_sample(cands, 50)) == 12


# --------------------------------------------------------------------------- run


def test_unlabeled_rows_are_skipped(tmp_path):
    path = tmp_path / "labels.yaml"
    path.write_text(
        yaml.safe_dump(
            [
                {"uid": "a", "verdict": "strong", "variant": "se", "note": ""},
                {"uid": "b", "verdict": "", "variant": "", "note": ""},
                {"uid": "c", "verdict": "No", "variant": "", "note": ""},
            ]
        )
    )
    labels, unlabeled = load_labels(path)
    assert labels == [Label("a", "strong", "se"), Label("c", "no", "")]
    assert unlabeled == 1


def test_invalid_label_is_rejected(tmp_path):
    path = tmp_path / "labels.yaml"
    path.write_text(yaml.safe_dump([{"uid": "a", "verdict": "great"}]))
    with pytest.raises(ValueError):
        load_labels(path)


def test_run_end_to_end_report_has_no_reasons(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    seed_store(db, 6)
    export(str(db), 6, tmp_path)
    entries = yaml.safe_load((tmp_path / "labels.yaml").read_text())
    verdicts = ["strong", "maybe", "no", "strong", "", ""]
    for entry, verdict in zip(entries, verdicts, strict=True):
        entry["verdict"] = verdict
        entry["variant"] = "se" if verdict else ""
    (tmp_path / "labels.yaml").write_text(yaml.safe_dump(entries))
    monkeypatch.setattr("jobhunt.evals.load_resumes", lambda: RESUMES)

    client = FakeClient([(90, "se"), (50, "fde"), (10, "se"), (60, "se")])
    path = run("claude-haiku-4-5", tmp_path, client=client)

    text = path.read_text()
    assert path.name == "claude-haiku-4-5.md"
    assert "SECRET-REASON-TEXT" not in text
    assert "RESUME-SENTINEL" not in text
    assert "Verdict agreement: 3/4 (75%)" in text
    assert "Variant accuracy: 3/4 (75%)" in text
    assert "2 unlabeled rows skipped" in text
    assert "Tokens: 4,000 input, 200 output" in text
    assert "Approximate cost: $0.005 total" in text
    assert all(call["model"] == "claude-haiku-4-5" for call in client.messages.calls)
    assert "SE-RESUME-SENTINEL" in client.messages.calls[0]["system"][0]["text"]


def test_jobs_roundtrip_gives_same_prompt_text(tmp_path):
    db = tmp_path / "jobs.db"
    seed_store(db, 3)
    export(str(db), 3, tmp_path)
    jobs = load_jobs(tmp_path / "jobs.jsonl")
    job = jobs["greenhouse:acme:0"]
    from jobhunt.scoring import html_to_text

    assert html_to_text(job.description_html) == "Build demos for customers. R&D 0 <fun>"
    assert job.pay_min == 100000 and job.remote is True


def test_failed_jobs_are_counted_not_quoted(tmp_path):
    jobs = {f"j{i}": make_job(i) for i in range(3)}
    labels = [Label(f"j{i}", "strong", "se") for i in range(3)]
    bad = SimpleNamespace(
        stop_reason="max_tokens",
        content=[SimpleNamespace(type="text", text="RAW-MODEL-OUTPUT")],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )

    class Odd(FakeMessages):
        def create(self, **kwargs):
            if not self.values:
                return bad
            return super().create(**kwargs)

    client = SimpleNamespace(messages=Odd([(80, "se")]))
    results = run_eval(client, jobs, labels, RESUMES, "claude-haiku-4-5")
    assert [r.score for r in results] == [80, None, None]
    report = render_report("claude-haiku-4-5", results, 0)
    assert "failed: 2" in report
    assert "RAW-MODEL-OUTPUT" not in report and "max_tokens" not in report


def test_unknown_model_has_no_cost():
    results = [Result("a", "strong", "se", 80, "se", 10, 5, 1.0)]
    report = render_report("some-new-model", results, 0)
    assert "Approximate cost: n/a" in report
    assert "Spearman rho (score vs verdict rank): n/a" in report
