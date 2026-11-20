"""Entry point: `claude_tools/po <command> ...` (see claude_tools/README.md)."""

import argparse
import os
import shutil
import sys

from claude_tools._common import REPO_ROOT, emit, gemini_configured

SKILL_SRC = os.path.join(REPO_ROOT, "claude_tools", "skill", "paper-orchestra", "SKILL.md")


def doctor(_args) -> int:
    import requests

    checks = {
        "gemini_credentials": gemini_configured(),
        "semantic_scholar_key": bool(os.getenv("SEMANTIC_SCHOLAR_API_KEY")),
        "pdflatex": bool(shutil.which("pdflatex")),
        "bibtex": bool(shutil.which("bibtex")),
    }
    try:
        import google.genai  # noqa: F401
        import pymupdf  # noqa: F401
        import thefuzz  # noqa: F401

        checks["python_deps"] = True
    except ImportError as e:
        checks["python_deps"] = f"missing: {e.name}"

    try:
        r = requests.get("https://api.semanticscholar.org/graph/v1/paper/search",
                         params={"query": "attention is all you need", "limit": 1, "fields": "title"},
                         headers={"x-api-key": os.environ["SEMANTIC_SCHOLAR_API_KEY"]}
                         if os.getenv("SEMANTIC_SCHOLAR_API_KEY") else {}, timeout=15)
        checks["semantic_scholar_http"] = r.status_code
    except Exception as e:  # noqa: BLE001
        checks["semantic_scholar_http"] = f"error: {e}"

    notes = []
    if not checks["gemini_credentials"]:
        notes.append("lit-review and review need GEMINI_API_KEY (or Vertex AI settings).")
    if not checks["semantic_scholar_key"]:
        notes.append("No SEMANTIC_SCHOLAR_API_KEY: verification works but is slower (shared rate limit, 429 retries).")
    if not checks["pdflatex"]:
        notes.append("pdflatex not found: compile PDFs elsewhere, or review .tex files directly.")
    ok = checks["python_deps"] is True and checks["gemini_credentials"]
    return emit({"ready": ok, "checks": checks, "notes": notes})


def install_skill(args) -> int:
    if args.user:
        target_root = os.path.expanduser("~/.claude/skills")
    else:
        target_root = os.path.join(os.path.abspath(args.project), ".claude", "skills")
    target = os.path.join(target_root, "paper-orchestra", "SKILL.md")
    with open(SKILL_SRC, "r", encoding="utf-8") as f:
        text = f.read().replace("{{PO}}", os.path.join(REPO_ROOT, "claude_tools", "po"))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(text)
    return emit({"installed": target})


def main(argv=None) -> int:
    from claude_tools import lit_review, review_paper

    parser = argparse.ArgumentParser(prog="po", description="PaperOrchestra tools for Claude Code")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("lit-review", help="Find and verify citations, build references.bib")
    lit_review.add_arguments(p)
    p.set_defaults(func=lit_review.run)

    p = sub.add_parser("review", help="Independent Gemini peer review (AgentReview) of a draft")
    review_paper.add_arguments(p)
    p.set_defaults(func=review_paper.run)

    p = sub.add_parser("doctor", help="Check credentials, dependencies and network")
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
