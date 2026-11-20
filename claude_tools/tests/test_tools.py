"""Offline tests: Gemini and Semantic Scholar are mocked.

Run: .venv/bin/python -m pytest claude_tools/tests -q
"""

import json
import os

import pytest

os.environ.setdefault("GEMINI_API_KEY", "test-dummy")

from claude_tools import arxiv, bib, cli, s2  # noqa: E402


def run_cli(argv, capsys):
    code = cli.main(argv)
    out = capsys.readouterr().out
    return code, json.loads(out)


# --------------------------------------------------------------------------- bib

def test_bibtex_escaping_and_arxiv():
    p = {"citation_key": "Doe2024Fast", "title": "Fast & Furious: 100% R_2", "authors": ["J. Doe"],
         "venue": "arXiv", "year": 2024, "arxiv_id": "2401.00001"}
    e = bib.render_entry(p)
    assert r"Fast \& Furious: 100\% R\_2" in e
    assert "journal={arXiv preprint arXiv:2401.00001}" in e
    assert e.startswith("@article{Doe2024Fast,")


def test_unique_key():
    assert bib.unique_key("A2020B", {"A2020B", "A2020Ba"}) == "A2020Bb"
    assert bib.base_key(["Ilya Loshchilov"], 2017, "Decoupled Weight Decay Regularization") == \
        "Loshchilov2017DecoupledWeight"


# --------------------------------------------------------------------------- lit-review

FAKE_S2 = {
    "decoupled weight decay regularization": {
        "title": "Decoupled Weight Decay Regularization", "year": 2017, "publicationDate": "2017-11-14",
        "authors": [{"name": "Ilya Loshchilov"}, {"name": "Frank Hutter"}],
        "venue": "International Conference on Learning Representations", "abstract": "AdamW.",
        "externalIds": {"ArXiv": "1711.05101"}, "paperId": "p1", "citationCount": 1,
    },
    "segment anything": {
        "title": "Segment Anything", "year": 2023, "publicationDate": "2023-04-05",
        "authors": [{"name": "Alexander Kirillov"}], "venue": "ICCV", "abstract": "SAM.",
        "externalIds": {}, "paperId": "p2",
    },
    "sam 2: segment anything in images and videos": {
        "title": "SAM 2: Segment Anything in Images and Videos", "year": 2024,
        "publicationDate": "2024-08-01", "authors": [{"name": "Nikhila Ravi"}], "venue": "ICLR",
        "abstract": "SAM2.", "externalIds": {}, "paperId": "p3",
    },
}


@pytest.fixture
def mocked(monkeypatch):
    from claude_tools._common import silenced_stdout

    with silenced_stdout():  # upstream prints the S2 key on import
        import methods.agents.literature_review_agent as lra

    calls = {"discover": 0}

    def fake_discover(self, task, outline, cutoff):
        calls["discover"] += 1
        if "AdamW" in task["focus"]:
            return [{"title": "Decoupled Weight Decay Regularization", "year": 2017, "reason": "optimizer"}]
        if "broken" in task["focus"]:
            raise RuntimeError("boom")
        return [
            {"title": "Segment Anything", "year": 2023, "reason": "foundation"},
            {"title": "SAM 2: Segment Anything in Images and Videos", "year": 2024, "reason": "video"},
            {"title": "Quantum Llamas", "year": 2024, "reason": "fake"},
            {"title": "Segment anything", "year": 2023, "reason": "dup"},
        ]

    def fake_search(title, year=None, cutoff=None, **_):
        p = FAKE_S2.get(title.lower())
        if p is None:
            return "not_found", None
        if not s2._before_cutoff(p.get("publicationDate"), p.get("year"), cutoff):
            return "after_cutoff", p
        return "verified", p

    monkeypatch.setattr(lra.HybridLiteratureAgent, "_discover_candidates", fake_discover)
    monkeypatch.setattr(s2, "search_title", fake_search)
    monkeypatch.setattr(arxiv, "search_title", lambda *a, **k: ("not_found", None))
    return calls


def test_lit_review_accumulates_and_keeps_keys(tmp_path, capsys, mocked):
    out = str(tmp_path / "lit")
    code, r = run_cli(["lit-review", "--out-dir", out, "--cutoff", "2024-06",
                       "--explore", "SAM video", "--find", "AdamW"], capsys)
    assert code == 0
    assert r["n_added"] == 2  # SAM + AdamW, SAM2 is after the cutoff
    assert r["candidate_status"]["after_cutoff"] == 1
    assert r["candidate_status"]["not_found"] == 1
    assert r["candidate_status"]["duplicate_candidate"] == 1
    keys_first = set(json.load(open(f"{out}/citation_map.json")))
    assert keys_first == {"Loshchilov2017DecoupledWeight", "Kirillov2023SegmentAnything"}

    bibtxt = open(f"{out}/references.bib").read()
    assert "arXiv preprint" not in bibtxt  # has a real venue
    assert "eprint={1711.05101}" in bibtxt

    # Second run with a later cutoff: SAM2 gets added, existing keys untouched.
    code, r = run_cli(["lit-review", "--out-dir", out, "--cutoff", "2025-01", "--explore", "SAM video"], capsys)
    assert code == 0
    assert r["n_added"] == 1 and r["registry_size"] == {"before": 2, "after": 3}
    assert r["candidate_status"]["already_in_registry"] == 1
    keys_second = set(json.load(open(f"{out}/citation_map.json")))
    assert keys_first < keys_second
    assert len(os.listdir(f"{out}/runs")) >= 1


def test_lit_review_reports_failed_tasks(tmp_path, capsys, mocked):
    code, r = run_cli(["lit-review", "--out-dir", str(tmp_path), "--explore", "broken query"], capsys)
    assert code == 1
    assert r["n_failed_tasks"] == 1 and "boom" in r["failed_tasks"][0]["error"]


def test_plan_formats(tmp_path, capsys):
    simple = tmp_path / "simple.json"
    simple.write_text(json.dumps({"problem": "p", "tasks": [
        {"section": "Introduction", "type": "explore", "query": "q1"},
        {"section": "Experiments", "type": "targeted", "query": "paper introducing 'ImageNet'"}]}))
    code, r = run_cli(["lit-review", "--out-dir", str(tmp_path), "--plan", str(simple), "--dry-run"], capsys)
    assert [t["search_type"] for t in r["tasks"]] == ["exploration", "targeted"]

    outline = tmp_path / "outline.json"
    outline.write_text(json.dumps({
        "intro_related_work_plan": {
            "introduction_strategy": {"search_directions": ["d1", "d2"], "problem_gap_hypothesis": "gap"},
            "related_work_strategy": {"subsections": [{"subsection_title": "2.1 X",
                                                       "limitation_search_queries": ["l1"]}]}},
        "section_plan": [{"section_title": "4. Exp", "subsections": [
            {"subsection_title": "4.1 Setup", "citation_hints": ["h1"]}]}]}))
    code, r = run_cli(["lit-review", "--out-dir", str(tmp_path), "--plan", str(outline), "--dry-run"], capsys)
    assert r["n_tasks"] == 4
    assert {t["section"] for t in r["tasks"]} == {"Introduction", "Related Work: 2.1 X", "4. Exp - 4.1 Setup"}


# --------------------------------------------------------------------------- review

def test_review_aggregates_scores(tmp_path, capsys, monkeypatch):
    from claude_tools import review_paper
    import autoraters.agent_review as ar

    overall = iter([4, 6, 8])

    def fake_single(text, model, temperature, system_prompt):
        assert "my paper body" in text and "Knowledgeability" in system_prompt
        return {"Overall": next(overall), "Soundness": 3, "Decision": "Reject", "Weaknesses": ["w"]}

    monkeypatch.setattr(review_paper, "_single_review", fake_single)
    monkeypatch.setattr(ar, "get_meta_review",
                        lambda m, t, rs: {"Overall": 1, "Decision": "Accept", "Weaknesses": ["meta"]})

    paper = tmp_path / "draft.tex"
    paper.write_text("my paper body")
    code, r = run_cli(["review", "--paper", str(paper)], capsys)
    assert code == 0
    assert r["scores"]["Overall"] == 6  # mean of individual scores overrides meta score
    assert r["overall_spread"]["values"] == [4, 6, 8]
    assert r["decision"] == "Accept" and r["weaknesses"] == ["meta"]
    saved = json.load(open(r["review_file"]))
    assert len(saved["individual_reviews"]) == 3


def test_review_missing_paper(capsys):
    code, r = run_cli(["review", "--paper", "/nonexistent.pdf"], capsys)
    assert code == 1 and "not found" in r["error"]


# --------------------------------------------------------------------------- verify (no LLM)

ARXIV_PAPER = {
    "arxiv_id": "2401.12345", "title": "Private Learning with Tiny Budgets", "abstract": "DP.",
    "authors": ["Ada Lovelace"], "published": "2024-01-20", "year": 2024, "journal_ref": None,
    "doi": None, "url": "https://arxiv.org/abs/2401.12345",
}


def test_verify_multi_source(tmp_path, capsys, monkeypatch):
    s2_answers = {
        "decoupled weight decay regularization": ("verified", FAKE_S2["decoupled weight decay regularization"]),
        "private learning with tiny budgets": ("s2_error", {"error": "HTTP 429"}),
        "flaky conference paper": ("s2_error", {"error": "HTTP 429"}),
        "sam 2: segment anything in images and videos":
            ("after_cutoff", FAKE_S2["sam 2: segment anything in images and videos"]),
    }
    arxiv_answers = {"private learning with tiny budgets": ("verified", ARXIV_PAPER)}
    monkeypatch.setattr(s2, "search_title", lambda t, *a, **k: s2_answers.get(t.lower(), ("not_found", None)))
    monkeypatch.setattr(arxiv, "search_title", lambda t, *a, **k: arxiv_answers.get(t.lower(), ("not_found", None)))

    cands = tmp_path / "cands.json"
    cands.write_text(json.dumps({"candidates": [
        {"title": "Decoupled Weight Decay Regularization", "year": 2017, "section": "Experiments"},
        {"title": "Private Learning with Tiny Budgets", "year": 2024, "source_url": "https://arxiv.org/abs/2401.12345"},
        {"title": "Flaky Conference Paper", "year": 2023},
        {"title": "SAM 2: Segment Anything in Images and Videos", "year": 2024},
        {"title": "Invented Paper"},
        {"no_title": True},
    ]}))
    out = str(tmp_path / "lit")
    code, r = run_cli(["verify", "--candidates", str(cands), "--out-dir", out, "--cutoff", "2024-06"], capsys)
    assert code == 0
    assert r["candidate_status"] == {"verified": 2, "lookup_error": 1, "after_cutoff": 1, "not_found": 1}
    assert {a["via"] for a in r["added"]} == {"s2", "arxiv"}
    assert r["retry_later"] == ["Flaky Conference Paper"] and r["not_found"] == ["Invented Paper"]

    papers = {p["citation_key"]: p for p in json.load(open(f"{out}/papers.json"))}
    assert papers["Lovelace2024PrivateLearning"]["verified_via"] == "arxiv"
    assert papers["Lovelace2024PrivateLearning"]["source_url"].endswith("2401.12345")
    bibtxt = open(f"{out}/references.bib").read()
    assert "journal={arXiv preprint arXiv:2401.12345}" in bibtxt

    # arXiv-only mode and --title shortcut
    code, r = run_cli(["verify", "--title", "Private Learning with Tiny Budgets", "--sources", "arxiv",
                       "--out-dir", out], capsys)
    assert r["candidate_status"] == {"already_in_registry": 1}

    code, r = run_cli(["verify", "--title", "x", "--sources", "scholar", "--out-dir", out], capsys)
    assert code == 1 and "unknown source" in r["error"]


def test_arxiv_parse_feed():
    xml = """<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/1610.05820v2</id>
    <published>2016-10-18T17:52:56Z</published>
    <title>Membership Inference Attacks against
      Machine Learning Models</title>
    <summary> We quantitatively investigate. </summary>
    <author><name>Reza Shokri</name></author>
    <author><name>Marco Stronati</name></author>
    <arxiv:journal_ref>IEEE S&amp;P 2017</arxiv:journal_ref>
  </entry>
</feed>"""
    [p] = arxiv.parse_feed(xml)
    assert p["arxiv_id"] == "1610.05820" and p["year"] == 2016 and p["published"] == "2016-10-18"
    assert p["title"] == "Membership Inference Attacks against Machine Learning Models"
    assert p["authors"] == ["Reza Shokri", "Marco Stronati"] and p["journal_ref"] == "IEEE S&P 2017"


# --------------------------------------------------------------------------- review (subagents)

def _review(overall, **extra):
    r = {"Summary": "s", "Strengths": ["a"], "Weaknesses": ["w"], "Questions": ["q"],
         "Overall": overall, "Soundness": 3, "Confidence": 4, "Decision": "Reject"}
    r.update(extra)
    return r


def test_native_review_flow(tmp_path, capsys):
    paper = tmp_path / "draft.md"
    paper.write_text("# My privacy paper\nWe propose a method.\n")
    rdir = tmp_path / "rev"

    code, r = run_cli(["review-prep", "--paper", str(paper), "--out-dir", str(rdir), "--no-benign"], capsys)
    assert code == 0
    steps = r["next_steps"]["1_spawn_in_parallel"]
    assert len(steps) == 3 and all(s["model"] == "opus" for s in steps)
    assert steps[0]["prompt"].count(str(rdir / "review_1.json")) == 1
    prompt = (rdir / "reviewer_prompt.md").read_text()
    assert "mean reviewer" in prompt and str(rdir / "paper.txt") in prompt
    assert "We propose a method." in (rdir / "paper.txt").read_text()

    # Two reviews written, one malformed -> asks to redo exactly those.
    (rdir / "review_1.json").write_text(json.dumps(_review(4)))
    (rdir / "review_2.json").write_text("Sure! ```json\n" + json.dumps(_review(6)) + "\n```")
    (rdir / "review_3.json").write_text(json.dumps({"Overall": 11}))
    code, r = run_cli(["review-aggregate", "--dir", str(rdir)], capsys)
    assert code == 1 and r["status"] == "incomplete_reviews" and r["valid_reviews"] == 2
    assert [s["description"] for s in r["next_steps"]["1_spawn_in_parallel"]] == [
        "Redo peer review review_3.json"]

    (rdir / "review_3.json").write_text(json.dumps(_review(8)))
    code, r = run_cli(["review-aggregate", "--dir", str(rdir)], capsys)
    assert code == 0 and r["status"] == "needs_meta_review"
    meta_prompt = (rdir / "meta_prompt.md").read_text()
    assert "area chair" in meta_prompt and "Review 3/3" in meta_prompt

    (rdir / "meta_review.json").write_text(json.dumps(_review(2, Decision="Accept", Weaknesses=["meta"])))
    code, r = run_cli(["review-aggregate", "--dir", str(rdir)], capsys)
    assert code == 0 and r["status"] == "done"
    assert r["scores"]["Overall"] == 6 and r["overall_spread"]["values"] == [4, 6, 8]
    assert r["decision"] == "Accept" and r["weaknesses"] == ["meta"]
    final = json.load(open(r["review_file"]))
    assert final["reviewer"] == "claude-code-subagents" and final["meta_review_used"] is True

    # A second prep into the same directory is refused.
    code, r = run_cli(["review-prep", "--paper", str(paper), "--out-dir", str(rdir)], capsys)
    assert code == 1


def test_native_review_single_no_meta(tmp_path, capsys):
    paper = tmp_path / "draft.txt"
    paper.write_text("text")
    rdir = tmp_path / "rev"
    run_cli(["review-prep", "--paper", str(paper), "--out-dir", str(rdir), "--ensemble", "1"], capsys)
    (rdir / "review_1.json").write_text(json.dumps(_review(5)))
    code, r = run_cli(["review-aggregate", "--dir", str(rdir)], capsys)
    assert code == 0 and r["status"] == "done" and r["scores"]["Overall"] == 5


# --------------------------------------------------------------------------- Windows specifics

def test_po_command_windows_path(monkeypatch):
    from claude_tools import _common

    monkeypatch.setattr(_common, "PO", r"C:\Users\Jie Ng\paper-orchestra\claude_tools\po")
    monkeypatch.setattr(_common.os, "name", "nt")
    assert _common.po_command() == '"C:/Users/Jie Ng/paper-orchestra/claude_tools/po"'


def test_utf8_output_on_legacy_console(tmp_path):
    """A cp950 console (Traditional Chinese Windows) must not crash on accented names."""
    import subprocess
    import sys

    cli_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cli.py")
    missing = tmp_path / "Tramèr_Martín.json"
    env = {**os.environ, "PYTHONIOENCODING": "cp950", "PYTHONUTF8": "0"}
    env.pop("PYTHONPATH", None)
    proc = subprocess.run([sys.executable, cli_path, "verify", "--candidates", str(missing),
                           "--out-dir", str(tmp_path)], capture_output=True, env=env, cwd=str(tmp_path))
    assert proc.returncode == 1, proc.stderr.decode("utf-8", "replace")
    assert "Tramèr_Martín" in json.loads(proc.stdout.decode("utf-8"))["error"]
