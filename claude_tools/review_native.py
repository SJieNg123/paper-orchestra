"""Claude-native peer review: `po review-prep` and `po review-aggregate`.

No API calls. The reviewing itself is done by fresh Claude Code subagents that
see only the paper, never the writing session:

  1. `po review-prep --paper main.pdf --out-dir D`
       extracts the paper text and writes the reviewer instructions
       (AgentReview persona + review form from autoraters/agent_review.py).
  2. Claude Code spawns N subagents in parallel, each writes D/review_<i>.json.
  3. `po review-aggregate --dir D`
       validates the reviews and, with N > 1, writes the area-chair prompt.
  4. One more subagent writes D/meta_review.json.
  5. `po review-aggregate --dir D` again produces D/final_review.json using the
     upstream rule (scores = rounded mean of the individual reviews).

Every step prints the exact next step in its JSON output ("next_steps").
"""

import argparse
import datetime
import glob
import json
import os
import re

from claude_tools._common import emit, po_command, log, read_json, stdout_to_stderr, write_json, write_text
from claude_tools.review_paper import SCORE_AXES, _score_stats, load_paper_text

DEFAULT_MODEL = "opus"

# Copied verbatim from ContentRefinementAgent._get_formatting_review (upstream
# builds it inline, so it cannot be imported).
FORMAT_PROMPT_HEAD = """
Analyze the provided page images of a research paper for formatting issues.
You must strictly follow the provided formatting guidelines and examples. Do not create rules based on your own assumptions or general knowledge if they are not explicitly supported by the guidelines or contradicted by the examples.

You must output the status for EVERY SINGLE figure and table found in the paper, and then list any other text content or layout issues.
Pay attention to column margins and page edges. Only report issues if a figure or table clearly overflows into the gutter or margin. Do not flag figures or tables that are simply filling the column width fully.

Formatting Guidelines:
"""
FORMAT_PROMPT_TAIL = """

Respond in JSON format with the following structure:
```json
{
  "figure_and_tables": {
    "Figure 1": {
      "detected_issue": "The figure is too wide and spills into the right margin.",
      "suggested_fix": "Use `[width=\\\\linewidth]` in `\\\\includegraphics` to scale the figure to the column width."
    },
    "Table 1": {
      "detected_issue": "None",
      "suggested_fix": "None"
    },
    # ...
  },
  "other_issues": [
    {
      "page": "Integer (page number starting from 1)",
      "element": "String ('Section x, Paragraph y' or other specific location details)",
      "detected_issue": "String describing the layout or text content issue",
      "suggested_fix": "String suggesting the fix"
    },
    # ...
  ]
}
```
"""

REQUIRED_FIELDS = ("Summary", "Strengths", "Weaknesses", "Overall", "Decision")


# --------------------------------------------------------------------------- arguments

def add_prep_arguments(p) -> None:
    p.add_argument("--paper", required=True, help="Draft to review: .pdf (preferred), .tex, .md or .txt")
    p.add_argument("--out-dir", help="Review directory (default: <paper_dir>/reviews/<time>)")
    p.add_argument("--ensemble", type=int, default=3, help="Number of independent reviewer subagents")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help="Model alias for the reviewer subagents, recorded and echoed in next_steps (default: opus)")
    p.add_argument("--knowledgeable", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--responsible", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--benign", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--format-check", action="store_true",
                   help="Also render page images for a layout check (needs a PDF and --guidelines)")
    p.add_argument("--guidelines", help="Conference guidelines.md for --format-check")


def add_aggregate_arguments(p) -> None:
    p.add_argument("--dir", required=True, help="Review directory created by review-prep")
    p.add_argument("--skip-meta", action="store_true",
                   help="Finalize without an area-chair meta-review (uses the first review's text)")


# --------------------------------------------------------------------------- helpers

def _q(path: str) -> str:
    path = path.replace("\\", "/") if os.name == "nt" else path
    return f'"{path}"' if " " in path else path


def _agent_step(model: str, prompt_file: str, output_file: str, what: str) -> dict:
    return {
        "tool": "Agent",
        "subagent_type": "general-purpose",
        "model": model,
        "description": what,
        "prompt": (f"Read {prompt_file} and follow its instructions exactly. "
                   f"Write your result to {output_file}. "
                   "Do not read any other project files and do not search the web."),
    }


def parse_review_file(path: str):
    """Load a review written by a subagent. Accepts plain JSON or JSON inside text/fences."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
    candidate = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        import json_repair  # type: ignore

        obj = json_repair.loads(candidate)
        return obj if isinstance(obj, dict) and obj else None


def _validate(review) -> list:
    if not isinstance(review, dict):
        return ["not a JSON object"]
    problems = [f"missing {k}" for k in REQUIRED_FIELDS if k not in review]
    for axis, (lo, hi) in SCORE_AXES:
        if axis in review:
            try:
                if not lo <= int(review[axis]) <= hi:
                    problems.append(f"{axis} out of range {lo}-{hi}")
            except (TypeError, ValueError):
                problems.append(f"{axis} is not a number")
    return problems


# --------------------------------------------------------------------------- review-prep

def run_prep(args) -> int:
    from autoraters.agent_review import AGENTREVIEW_INSTRUCTIONS, get_agentreview_system_prompt

    paper = os.path.abspath(args.paper)
    if not os.path.exists(paper):
        return emit({"error": f"paper not found: {paper}"}, 1)
    if args.format_check and not (paper.lower().endswith(".pdf") and args.guidelines):
        return emit({"error": "--format-check needs a .pdf paper and --guidelines"}, 1)
    if args.ensemble < 1:
        return emit({"error": "--ensemble must be >= 1"}, 1)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.abspath(args.out_dir or os.path.join(os.path.dirname(paper), "reviews", stamp))
    if glob.glob(os.path.join(out_dir, "review_*.json")):
        return emit({"error": f"{out_dir} already contains reviews, use a new --out-dir"}, 1)

    try:
        with stdout_to_stderr():
            text = load_paper_text(paper)
    except Exception as e:  # noqa: BLE001
        return emit({"error": f"could not extract text: {type(e).__name__}: {e}"}, 1)

    paper_txt = os.path.join(out_dir, "paper.txt")
    write_text(paper_txt, text)
    n_lines = text.count("\n") + 1

    persona = {"is_knowledgeable": args.knowledgeable, "is_responsible": args.responsible,
               "is_benign": args.benign}
    reviewer_prompt = os.path.join(out_dir, "reviewer_prompt.md")
    write_text(reviewer_prompt, "\n".join([
        "# Role",
        "",
        get_agentreview_system_prompt(**persona),
        "",
        "# Task",
        "",
        f"Review the paper in `{paper_txt}` ({n_lines} lines). Read ALL of it before writing; "
        "if the file is long, read it in several parts with offset/limit.",
        "Judge only what the paper itself says. Do not look up the paper, its authors or related "
        "files, and do not use web search.",
        AGENTREVIEW_INSTRUCTIONS,
        "",
        "# Output file",
        "",
        "Write ONLY the REVIEW JSON object (no THOUGHT section, no code fences, no other text) to "
        "the output file you were given. Numeric fields must be integers within their stated ranges. "
        "Reply with one line confirming the file was written.",
    ]))

    steps = [_agent_step(args.model, reviewer_prompt, os.path.join(out_dir, f"review_{i}.json"),
                         f"Peer review {i}/{args.ensemble}")
             for i in range(1, args.ensemble + 1)]

    files = {"paper_text": paper_txt, "reviewer_prompt": reviewer_prompt}
    if args.format_check:
        from utils.pdf_utils import pdf_to_grid_images

        with open(args.guidelines, "r", encoding="utf-8") as f:
            guidelines = f.read()
        with stdout_to_stderr():
            grids = pdf_to_grid_images(paper, os.path.join(out_dir, "pages"))
        format_prompt = os.path.join(out_dir, "format_prompt.md")
        write_text(format_prompt, "\n".join([
            "Look at each of these page images (each image is a 2x2 grid of consecutive pages, "
            "read left to right, top to bottom):",
            *[f"- {g}" for g in grids],
            FORMAT_PROMPT_HEAD + guidelines + FORMAT_PROMPT_TAIL,
            "Write ONLY the JSON object (no code fences) to the output file you were given.",
        ]))
        files.update(format_prompt=format_prompt, page_grids=grids)
        steps.append(_agent_step(args.model, format_prompt, os.path.join(out_dir, "format_review.json"),
                                 "Layout check"))

    write_json(os.path.join(out_dir, "manifest.json"), {
        "paper": paper, "created_at": stamp, "ensemble": args.ensemble, "model": args.model,
        "persona": persona, "format_check": bool(args.format_check), "files": files,
    })
    return emit({
        "review_dir": out_dir,
        "files": files,
        "next_steps": {
            "1_spawn_in_parallel": steps,
            "2_then_run": f"{po_command()} review-aggregate --dir {_q(out_dir)}",
        },
    })


# --------------------------------------------------------------------------- review-aggregate

def _summary(final, stats, n, record_path, fmt):
    out = {
        "review_file": record_path,
        "decision": final.get("Decision"),
        "scores": {a: final.get(a) for a, _ in SCORE_AXES if a in final},
        "overall_spread": stats.get("Overall"),
        "weaknesses": final.get("Weaknesses"),
        "questions": final.get("Questions"),
        "n_reviews": n,
    }
    if fmt is not None:
        out["formatting_review"] = fmt
    return out


def run_aggregate(args) -> int:
    from autoraters.agent_review import AGENTREVIEW_INSTRUCTIONS, meta_reviewer_system_prompt

    d = os.path.abspath(args.dir)
    manifest = read_json(os.path.join(d, "manifest.json"))
    if manifest is None:
        return emit({"error": f"no manifest.json in {d}, run review-prep first"}, 1)

    paths = sorted(glob.glob(os.path.join(d, "review_*.json")),
                   key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)) or 0))
    reviews, invalid = [], {}
    for p in paths:
        try:
            r = parse_review_file(p)
        except Exception as e:  # noqa: BLE001
            invalid[p] = [f"unreadable: {e}"]
            continue
        problems = _validate(r)
        if problems:
            invalid[p] = problems
        else:
            reviews.append(r)

    expected = manifest.get("ensemble", len(paths))
    model = manifest.get("model", DEFAULT_MODEL)
    if invalid or len(reviews) < expected:
        missing = [os.path.join(d, f"review_{i}.json") for i in range(1, expected + 1)
                   if not os.path.exists(os.path.join(d, f"review_{i}.json"))]
        redo = missing + list(invalid)
        return emit({
            "status": "incomplete_reviews",
            "valid_reviews": len(reviews), "expected": expected,
            "invalid": invalid, "missing": missing,
            "next_steps": {
                "1_spawn_in_parallel": [
                    _agent_step(model, manifest["files"]["reviewer_prompt"], p,
                                f"Redo peer review {os.path.basename(p)}") for p in redo],
                "2_then_run": f"{po_command()} review-aggregate --dir {_q(d)}",
            },
        }, 1)

    stats = _score_stats(reviews)
    meta_path = os.path.join(d, "meta_review.json")
    meta = None
    if len(reviews) > 1 and not args.skip_meta:
        if not os.path.exists(meta_path):
            meta_prompt = os.path.join(d, "meta_prompt.md")
            review_text = "".join(
                f"\nReview {i + 1}/{len(reviews)}:\n```\n{json.dumps(r, ensure_ascii=False)}\n```\n"
                for i, r in enumerate(reviews))
            write_text(meta_prompt, "\n".join([
                "# Role", "", meta_reviewer_system_prompt.format(reviewer_count=len(reviews)), "",
                "# Task", "", AGENTREVIEW_INSTRUCTIONS + review_text, "",
                "# Output file", "",
                "Write ONLY the REVIEW JSON object (no THOUGHT section, no code fences, no other text) "
                "to the output file you were given. Reply with one line confirming the file was written.",
            ]))
            return emit({
                "status": "needs_meta_review",
                "valid_reviews": len(reviews),
                "score_stats": stats,
                "next_steps": {
                    "1_spawn": [_agent_step(model, meta_prompt, meta_path, "Area-chair meta-review")],
                    "2_then_run": f"{po_command()} review-aggregate --dir {_q(d)}",
                },
            })
        try:
            meta = parse_review_file(meta_path)
        except Exception as e:  # noqa: BLE001
            meta = None
            log(f"meta_review.json unreadable: {e}")
        if _validate(meta):
            os.remove(meta_path)
            return emit({
                "status": "invalid_meta_review",
                "problems": _validate(meta),
                "next_steps": {
                    "1_spawn": [_agent_step(model, os.path.join(d, "meta_prompt.md"), meta_path,
                                            "Redo area-chair meta-review")],
                    "2_then_run": f"{po_command()} review-aggregate --dir {_q(d)}",
                },
            }, 1)

    final = dict(meta or reviews[0])
    if len(reviews) > 1:
        for axis, s in stats.items():  # upstream rule: rounded mean of individual reviews
            final[axis] = int(round(s["mean"]))

    fmt = None
    pending = []
    if manifest.get("format_check"):
        fmt_path = os.path.join(d, "format_review.json")
        if os.path.exists(fmt_path):
            try:
                fmt = parse_review_file(fmt_path)
            except Exception as e:  # noqa: BLE001
                pending.append(f"format_review.json unreadable: {e}")
        else:
            pending.append("format_review.json missing (layout check subagent not run)")

    record_path = os.path.join(d, "final_review.json")
    write_json(record_path, {
        "paper": manifest["paper"],
        "reviewed_at": manifest.get("created_at"),
        "reviewer": "claude-code-subagents",
        "model": model,
        "persona": manifest.get("persona"),
        "ensemble": len(reviews),
        "review": final,
        "score_stats": stats,
        "individual_reviews": reviews,
        "meta_review_used": meta is not None,
        "formatting_review": fmt,
    })
    result = {"status": "done"}
    result.update(_summary(final, stats, len(reviews), record_path, fmt))
    if pending:
        result["pending"] = pending
    return emit(result)
