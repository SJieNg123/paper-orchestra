"""`po lit-review`: verified literature search for Claude Code.

Wraps upstream `HybridLiteratureAgent`:
  Phase 1a  Gemini + Google Search grounding proposes candidate papers per task
            (upstream prompt, unchanged).
  Phase 1b  Each candidate is verified on Semantic Scholar and filtered by the
            research cutoff date. Only verified papers enter the registry.
  Optional  Gemini drafts Introduction + Related Work from the registry
            (upstream writer prompt), as a baseline Claude Code can compare with.

Unlike the upstream pipeline, results accumulate in `<out_dir>/papers.json`
across calls, existing citation keys never change, and every dropped candidate
is reported with a reason.
"""

import datetime
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from claude_tools import bib
from claude_tools import registry as reg
from claude_tools._common import (
    default_cutoff,
    emit,
    gemini_configured,
    log,
    read_json,
    silenced_stdout,
    stdout_to_stderr,
    write_json,
    write_text,
)

UPSTREAM_SEARCH_MODEL = "gemini-3-flash-preview"


def add_arguments(p) -> None:
    src = p.add_argument_group("what to search (combine freely)")
    src.add_argument("--plan", help="JSON search plan: simple {'problem','tasks'} format or an upstream outline.json")
    src.add_argument("--find", action="append", default=[], metavar="HINT",
                     help="Targeted lookup of ONE canonical paper, e.g. \"research paper introducing 'AdamW'\". Repeatable.")
    src.add_argument("--explore", action="append", default=[], metavar="QUERY",
                     help="Exploratory search returning ~10-15 relevant papers. Repeatable.")
    src.add_argument("--section", default="Related Work", help="Section label recorded for --find/--explore papers")
    src.add_argument("--context", default="", help="Extra context for --find/--explore tasks")
    src.add_argument("--problem", default="", help="One-line problem statement given to exploratory searches")

    p.add_argument("--out-dir", required=True, help="Registry directory (created if missing, reused across calls)")
    p.add_argument("--cutoff", default=None, help="Only papers published before YYYY-MM (default: current month)")
    p.add_argument("--search-model", default=UPSTREAM_SEARCH_MODEL, help="Gemini model with Google Search grounding")
    p.add_argument("--workers", type=int, default=8, help="Parallel Gemini search calls")
    p.add_argument("--sources", default="s2,arxiv", help="Verification lookup order: s2, arxiv (default: s2,arxiv)")
    p.add_argument("--dry-run", action="store_true", help="Only parse and print the search tasks")

    draft = p.add_argument_group("optional Gemini draft of Introduction + Related Work")
    draft.add_argument("--write-draft", action="store_true")
    draft.add_argument("--template", help="template.tex whose Intro/Related Work sections get filled")
    draft.add_argument("--idea", help="idea.md")
    draft.add_argument("--experimental-log", help="experimental_log.md")
    draft.add_argument("--guidelines", help="conference guidelines.md")
    draft.add_argument("--writer-model", default="gemini-3.1-pro-preview")


# --------------------------------------------------------------------------- tasks

def _simple_plan_tasks(plan: Dict) -> List[Dict]:
    tasks = []
    for t in plan.get("tasks", []):
        kind = str(t.get("type", "explore")).lower()
        query = t.get("query") or t.get("focus")
        if not query:
            continue
        tasks.append({
            "section": t.get("section", "Related Work"),
            "focus": query,
            "context": t.get("context", ""),
            "search_type": "targeted" if kind.startswith("target") or kind == "find" else "exploration",
        })
    return tasks


def build_tasks(args, agent_cls) -> Tuple[List[Dict], Dict, Optional[Dict]]:
    """Return (tasks, outline_for_prompts, upstream_outline_or_None)."""
    tasks: List[Dict] = []
    problem = args.problem
    upstream_outline = None

    if args.plan:
        plan = read_json(args.plan)
        if plan is None:
            raise FileNotFoundError(f"plan not found: {args.plan}")
        if "intro_related_work_plan" in plan or "section_plan" in plan:
            upstream_outline = plan
            # _collect_search_tasks only reads the outline, no instance state needed.
            tasks.extend(agent_cls._collect_search_tasks(None, plan))
            problem = problem or (
                plan.get("intro_related_work_plan", {})
                .get("introduction_strategy", {})
                .get("problem_gap_hypothesis", "")
            )
        else:
            tasks.extend(_simple_plan_tasks(plan))
            problem = problem or plan.get("problem", "")

    for hint in args.find:
        tasks.append({"section": args.section, "focus": hint,
                      "context": args.context or f"Must-have citation for {args.section}",
                      "search_type": "targeted"})
    for q in args.explore:
        tasks.append({"section": args.section, "focus": q, "context": args.context,
                      "search_type": "exploration"})

    seen, unique = set(), []
    for t in tasks:
        k = (t["section"], t["focus"], t["search_type"])
        if k not in seen:
            seen.add(k)
            unique.append(t)

    outline = upstream_outline or {
        "intro_related_work_plan": {"introduction_strategy": {"problem_gap_hypothesis": problem or "N/A"}}
    }
    return unique, outline, upstream_outline


# --------------------------------------------------------------------------- phases

def _cand_fields(c) -> Tuple[str, Optional[int], str]:
    get = c.get if isinstance(c, dict) else (lambda k, d=None: getattr(c, k, d))
    title = (get("title") or "").strip()
    try:
        year = int(get("year")) if get("year") else None
    except (TypeError, ValueError):
        year = None
    return title, year, get("reason") or ""


def discover(agent, tasks, outline, cutoff, workers) -> Tuple[List[Dict], List[Dict]]:
    """Phase 1a. Returns (candidates, task_reports)."""

    def run(task):
        last_err = None
        for _ in range(3):  # upstream also retries empty results 3 times
            try:
                cands = agent._discover_candidates(task, outline, cutoff)
                if cands:
                    return cands, None
                last_err = "no candidates returned"
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
        return [], last_err

    candidates, reports = [], []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(run, t): t for t in tasks}
        for fut in as_completed(futs):
            task = futs[fut]
            cands, err = fut.result()
            reports.append({"section": task["section"], "type": task["search_type"],
                            "query": task["focus"], "n_candidates": len(cands), "error": err})
            log(f"  [{task['search_type']}] {task['focus'][:70]!r}: {len(cands)} candidates"
                + (f" (error: {err[:120]})" if err else ""))
            for c in cands:
                title, year, reason = _cand_fields(c)
                if title:
                    candidates.append({"title": title, "year": year, "reason": reason,
                                       "section": task["section"], "query": task["focus"],
                                       "search_type": task["search_type"], "source_url": None})
    return candidates, reports


# --------------------------------------------------------------------------- main

def _patch_search_model(module, model: str) -> None:
    """Upstream hard-codes the search model inside _discover_candidates; swap it here."""
    if model == UPSTREAM_SEARCH_MODEL:
        return
    original = module.call_gemini_with_contents

    def patched(*a, **kw):
        if kw.get("model_name") == UPSTREAM_SEARCH_MODEL:
            kw["model_name"] = model
        return original(*a, **kw)

    module.call_gemini_with_contents = patched


def run(args) -> int:
    cutoff = args.cutoff or default_cutoff()
    out_dir = os.path.abspath(args.out_dir)

    with silenced_stdout():  # upstream scholar_utils prints the S2 API key on import
        import methods.agents.literature_review_agent as lra

    tasks, outline, upstream_outline = build_tasks(args, lra.HybridLiteratureAgent)
    if not tasks:
        return emit({"error": "no search tasks: pass --plan, --find or --explore"}, 1)

    if args.dry_run:
        return emit({"dry_run": True, "cutoff": cutoff, "n_tasks": len(tasks), "tasks": tasks})

    if not gemini_configured():
        return emit({"error": "Gemini is not configured. Set GEMINI_API_KEY (or VERTEX_AI_PROJECT + "
                              "VERTEX_AI_LOCATION) in the environment or in the repo's .env file."}, 1)
    if args.write_draft and not args.template:
        return emit({"error": "--write-draft needs --template"}, 1)

    try:
        sources = reg.parse_sources(args.sources)
    except ValueError as e:
        return emit({"error": str(e)}, 1)
    registry = reg.Registry(out_dir)

    _patch_search_model(lra, args.search_model)
    started = datetime.datetime.now()

    with stdout_to_stderr():
        agent = lra.HybridLiteratureAgent(
            idea_path=args.idea or "",
            experimental_log_path=args.experimental_log or "",
            latex_template_path=args.template or "",
            conference_guidelines_path=args.guidelines or "",
            output_dir=out_dir,
            model_name=args.writer_model,
        )
        log(f"Phase 1a: {len(tasks)} search tasks via {args.search_model} (cutoff {cutoff})")
        candidates, task_reports = discover(agent, tasks, outline, cutoff, args.workers)
        log(f"Phase 1b: verifying {len(candidates)} candidates via {'+'.join(sources)}")
        outcomes = registry.verify(candidates, cutoff, sources)

    papers = registry.papers
    files = registry.save({
        "command": "lit-review",
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "cutoff": cutoff,
        "search_model": args.search_model,
        "sources": list(sources),
        "tasks": task_reports,
        "candidates": outcomes,
    })

    draft_error = None
    if args.write_draft:
        log(f"Writing Intro + Related Work draft with {args.writer_model}")
        try:
            with stdout_to_stderr():
                paper_objs = [lra.PaperData(**{**p, "reason": p.get("reason") or "",
                                               "citation_count": p.get("citation_count") or 0,
                                               "venue": p.get("venue") or "arXiv",
                                               "year": p.get("year") or 0}) for p in papers]
                if upstream_outline is not None:
                    by_title = {bib.normalize_title(p.title): p for p in paper_objs}
                    upstream_outline = agent._inject_citations_into_outline(upstream_outline, by_title)
                    outline_path = os.path.join(out_dir, "outline_with_citations.json")
                    write_json(outline_path, upstream_outline)
                    files["outline_with_citations"] = outline_path
                latex = agent._synthesize_content(upstream_outline or outline, paper_objs, cutoff)
            draft_path = os.path.join(out_dir, "intro_related_work_draft.tex")
            write_text(draft_path, latex or "")
            files["draft_tex"] = draft_path
        except Exception as e:  # noqa: BLE001
            draft_error = f"{type(e).__name__}: {e}"

    failed_tasks = [t for t in task_reports if t["error"] and t["n_candidates"] == 0]
    result = {"out_dir": out_dir, "cutoff": cutoff, "n_tasks": len(tasks),
              "n_failed_tasks": len(failed_tasks)}
    result.update(reg.summarize(outcomes, registry))
    result["files"] = files
    if failed_tasks:
        result["failed_tasks"] = [{"query": t["query"], "error": t["error"]} for t in failed_tasks]
    if draft_error:
        result["draft_error"] = draft_error

    all_failed = tasks and len(failed_tasks) == len(tasks)
    if all_failed:
        result["error"] = "every search task failed, see failed_tasks"
    return emit(result, 1 if all_failed or draft_error else 0)
