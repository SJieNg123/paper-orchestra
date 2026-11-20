"""`po review`: independent Gemini peer review of a draft.

Uses the upstream AgentReview reviewer (autoraters/agent_review.py): same
persona prompt, same review form, same ensemble + area-chair meta-review, and
the same rule that numeric scores are the rounded mean of the individual
reviews. Differences from calling `perform_review_agentreview` directly:
individual reviews run in parallel and are all saved, per-axis score spread is
reported, and an optional VLM formatting check (from ContentRefinementAgent)
can run on the rendered PDF pages.
"""

import argparse
import datetime
import os
import statistics
from concurrent.futures import ThreadPoolExecutor

from claude_tools._common import (
    emit,
    gemini_configured,
    log,
    stdout_to_stderr,
    write_json,
)

SCORE_AXES = [
    ("Originality", (1, 4)), ("Quality", (1, 4)), ("Clarity", (1, 4)),
    ("Significance", (1, 4)), ("Soundness", (1, 4)), ("Presentation", (1, 4)),
    ("Contribution", (1, 4)), ("Overall", (1, 10)), ("Confidence", (1, 5)),
]


def add_arguments(p) -> None:
    p.add_argument("--paper", required=True, help="Draft to review: .pdf (preferred), .tex, .md or .txt")
    p.add_argument("--out", help="Where to save the review JSON (default: <paper_dir>/reviews/review_<time>.json)")
    p.add_argument("--model", default="gemini-3.1-pro-preview")
    p.add_argument("--ensemble", type=int, default=3, help="Independent reviews to aggregate (1 = single review)")
    p.add_argument("--temperature", type=float, default=0.75)
    p.add_argument("--knowledgeable", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--responsible", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--benign", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--format-check", action="store_true",
                   help="Also run the VLM layout check on rendered pages (needs a PDF and --guidelines)")
    p.add_argument("--guidelines", help="Conference guidelines.md for --format-check")


def load_paper_text(path: str) -> str:
    if path.lower().endswith(".pdf"):
        from utils.pdf_utils import load_paper

        return load_paper(path, min_size=100)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _single_review(text, model, temperature, system_prompt):
    from autoraters.agent_review import AGENTREVIEW_INSTRUCTIONS
    from utils.gemini_utils import call_gemini_with_text_prompt

    prompt = AGENTREVIEW_INSTRUCTIONS + f"\nHere is the paper you are asked to review:\n```\n{text}\n```"
    rsp = call_gemini_with_text_prompt(
        prompt=prompt,
        model_name=model,
        generation_configs={"temperature": temperature, "system_instruction": system_prompt},
    )
    return rsp["parsed_response"]


def _score_stats(reviews):
    stats = {}
    for axis, (lo, hi) in SCORE_AXES:
        vals = []
        for r in reviews:
            try:
                v = int(r.get(axis))
            except (TypeError, ValueError, AttributeError):
                continue
            if lo <= v <= hi:
                vals.append(v)
        if vals:
            stats[axis] = {
                "mean": round(statistics.mean(vals), 2),
                "std": round(statistics.pstdev(vals), 2),
                "values": vals,
            }
    return stats


def review(text, model, ensemble, temperature, persona):
    from autoraters.agent_review import get_agentreview_system_prompt, get_meta_review

    system_prompt = get_agentreview_system_prompt(**persona)
    n = max(1, ensemble)
    log(f"Running {n} review(s) with {model}")
    with ThreadPoolExecutor(max_workers=n) as ex:
        futs = [ex.submit(_single_review, text, model, temperature, system_prompt) for _ in range(n)]
        individual, errors = [], []
        for f in futs:
            try:
                r = f.result()
                if isinstance(r, dict):
                    individual.append(r)
                else:
                    errors.append("unparseable review")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}")
    if not individual:
        raise RuntimeError("all reviews failed: " + "; ".join(errors))

    stats = _score_stats(individual)
    if len(individual) == 1:
        final = dict(individual[0])
    else:
        log("Aggregating with area-chair meta-review")
        try:
            final = get_meta_review(model, temperature, individual) or dict(individual[0])
        except Exception as e:  # noqa: BLE001
            errors.append(f"meta-review failed, using first review: {e}")
            final = dict(individual[0])
        # Upstream rule: scores are the rounded mean of individual reviews.
        for axis, s in stats.items():
            final[axis] = int(round(s["mean"]))
    return final, individual, stats, errors


def format_check(pdf_path, guidelines, model, work_dir):
    from methods.agents.content_refinement_agent import ContentRefinementAgent

    agent = ContentRefinementAgent(
        experimental_log_path="", citation_map_path="", guidelines_path=guidelines,
        model_name=model, max_reflections=0, work_dir=work_dir,
    )
    return agent._get_formatting_review(pdf_path, "format_0")


def run(args) -> int:
    paper = os.path.abspath(args.paper)
    if not os.path.exists(paper):
        return emit({"error": f"paper not found: {paper}"}, 1)
    if not gemini_configured():
        return emit({"error": "Gemini is not configured. Set GEMINI_API_KEY (or VERTEX_AI_PROJECT + "
                              "VERTEX_AI_LOCATION) in the environment or in the repo's .env file."}, 1)
    if args.format_check and not (paper.lower().endswith(".pdf") and args.guidelines):
        return emit({"error": "--format-check needs a .pdf paper and --guidelines"}, 1)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.abspath(args.out or os.path.join(os.path.dirname(paper), "reviews", f"review_{stamp}.json"))
    persona = {"is_knowledgeable": args.knowledgeable, "is_responsible": args.responsible,
               "is_benign": args.benign}

    try:
        with stdout_to_stderr():
            text = load_paper_text(paper)
            final, individual, stats, errors = review(text, args.model, args.ensemble, args.temperature, persona)
            fmt = None
            if args.format_check:
                log("Running VLM formatting check")
                fmt = format_check(paper, os.path.abspath(args.guidelines), args.model,
                                   os.path.join(os.path.dirname(out), f"format_check_{stamp}"))
    except Exception as e:  # noqa: BLE001
        return emit({"error": f"{type(e).__name__}: {e}", "paper": paper}, 1)

    record = {
        "paper": paper,
        "reviewed_at": stamp,
        "model": args.model,
        "persona": persona,
        "ensemble": args.ensemble,
        "temperature": args.temperature,
        "review": final,
        "score_stats": stats,
        "individual_reviews": individual,
        "formatting_review": fmt,
        "errors": errors,
    }
    write_json(out, record)

    score_keys = [a for a, _ in SCORE_AXES]
    result = {
        "review_file": out,
        "decision": final.get("Decision"),
        "scores": {k: final.get(k) for k in score_keys if k in final},
        "overall_spread": stats.get("Overall"),
        "weaknesses": final.get("Weaknesses"),
        "questions": final.get("Questions"),
        "n_reviews": len(individual),
    }
    if fmt is not None:
        result["formatting_review"] = fmt
    if errors:
        result["warnings"] = errors
    return emit(result)
