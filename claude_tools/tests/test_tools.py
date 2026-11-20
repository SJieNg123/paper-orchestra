"""Offline tests: Gemini and Semantic Scholar are mocked.

Run: .venv/bin/python -m pytest claude_tools/tests -q
"""

import json
import os

import pytest

os.environ.setdefault("GEMINI_API_KEY", "test-dummy")

from claude_tools import bib, cli, s2  # noqa: E402


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
