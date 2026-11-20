"""Entry point: `claude_tools/po <command> ...` (see claude_tools/README.md)."""

import argparse
import importlib.util
import os
import shutil
import sys

from claude_tools._common import PO, REPO_ROOT, emit, gemini_configured

SKILL_SRC = os.path.join(REPO_ROOT, "claude_tools", "skill", "paper-orchestra", "SKILL.md")


def _http_status(url, params):
    import requests

    try:
        return requests.get(url, params=params, timeout=20).status_code
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def doctor(_args) -> int:
    missing = [m for m in ("pymupdf", "thefuzz", "requests", "json_repair", "google.genai")
               if importlib.util.find_spec(m.split(".")[0]) is None]
    checks = {
        "python_deps": True if not missing else f"missing: {', '.join(missing)}",
        "semantic_scholar_http": _http_status(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            {"query": "attention is all you need", "limit": 1, "fields": "title"}),
        "semantic_scholar_key_env": bool(os.getenv("SEMANTIC_SCHOLAR_API_KEY")),
        "arxiv_http": _http_status("https://export.arxiv.org/api/query",
                                   {"search_query": 'ti:"segment anything"', "max_results": 1}),
        "pdflatex": bool(shutil.which("pdflatex")),
        "gemini_credentials": gemini_configured(),
    }
    notes = []
    if checks["semantic_scholar_http"] == 429:
        notes.append("Semantic Scholar returned 429 (rate limited). Without a working key verification "
                     "is slow and falls back to arXiv. The key can come from SEMANTIC_SCHOLAR_API_KEY or "
                     "be injected by a network proxy.")
    if checks["arxiv_http"] != 200:
        notes.append("arXiv API not reachable: only Semantic Scholar will be used.")
    if not checks["pdflatex"]:
        notes.append("pdflatex not found: compile PDFs elsewhere, or review .tex files directly.")
    if not checks["gemini_credentials"]:
        notes.append("No Gemini credentials: only the legacy lit-review/review commands need them.")
    ready = checks["python_deps"] is True and (checks["semantic_scholar_http"] == 200
                                               or checks["arxiv_http"] == 200)
    return emit({"ready": ready, "checks": checks, "notes": notes})


def install_skill(args) -> int:
    if args.user:
        target_root = os.path.expanduser("~/.claude/skills")
    else:
        target_root = os.path.join(os.path.abspath(args.project), ".claude", "skills")
    target = os.path.join(target_root, "paper-orchestra", "SKILL.md")
    with open(SKILL_SRC, "r", encoding="utf-8") as f:
        text = f.read().replace("{{PO}}", PO)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(text)
    return emit({"installed": target})


def main(argv=None) -> int:
    from claude_tools import lit_review, review_native, review_paper, verify

    parser = argparse.ArgumentParser(prog="po", description="PaperOrchestra tools for Claude Code")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("verify", help="Verify candidate papers (Semantic Scholar + arXiv), update references.bib")
    verify.add_arguments(p)
    p.set_defaults(func=verify.run)

    p = sub.add_parser("review-prep", help="Prepare an AgentReview review run for Claude subagents")
    review_native.add_prep_arguments(p)
    p.set_defaults(func=review_native.run_prep)

    p = sub.add_parser("review-aggregate", help="Validate and aggregate subagent reviews")
    review_native.add_aggregate_arguments(p)
    p.set_defaults(func=review_native.run_aggregate)

    p = sub.add_parser("lit-review", help="[Gemini API] search with Gemini + Google Search, then verify")
    lit_review.add_arguments(p)
    p.set_defaults(func=lit_review.run)

    p = sub.add_parser("review", help="[Gemini API] AgentReview review by Gemini")
    review_paper.add_arguments(p)
    p.set_defaults(func=review_paper.run)

    p = sub.add_parser("doctor", help="Check dependencies and API reachability")
    p.set_defaults(func=doctor)

    p = sub.add_parser("install-skill", help="Install the Claude Code skill into a project or ~/.claude")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--project", help="Project root to install into (<project>/.claude/skills)")
    g.add_argument("--user", action="store_true", help="Install for all projects (~/.claude/skills)")
    p.set_defaults(func=install_skill)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
