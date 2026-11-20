"""`po verify`: verify candidate papers and add them to the citation registry.

No LLM involved. Claude Code finds candidates itself (WebSearch / WebFetch),
writes them to a JSON file, and this command checks each title against
Semantic Scholar and/or arXiv, applies the cutoff date, and regenerates
references.bib + citation_map.json.

Candidates file: either a list or {"candidates": [...]}, each item
  {"title": "...", "year": 2023, "section": "Related Work: X",
   "query": "what was searched", "reason": "why it is relevant",
   "source_url": "where it was found"}
Only "title" is required.
"""

import datetime
import os

from claude_tools import registry as reg
from claude_tools._common import default_cutoff, emit, log, read_json


def add_arguments(p) -> None:
    p.add_argument("--candidates", help="JSON file with candidate papers (see module docstring)")
    p.add_argument("--title", action="append", default=[], help="Verify a single title. Repeatable.")
    p.add_argument("--year", type=int, help="Year hint for --title (only when one --title is given)")
    p.add_argument("--section", default="Related Work", help="Default section label")
    p.add_argument("--out-dir", required=True, help="Registry directory (created if missing, reused across calls)")
    p.add_argument("--cutoff", default=None, help="Only papers published before YYYY-MM (default: current month)")
    p.add_argument("--sources", default="s2,arxiv",
                   help="Comma-separated lookup order: s2, arxiv (default: s2,arxiv)")


def load_candidates(args):
    raw = []
    if args.candidates:
        data = read_json(args.candidates)
        if data is None:
            raise FileNotFoundError(f"candidates file not found: {args.candidates}")
        raw.extend(data.get("candidates", []) if isinstance(data, dict) else data)
    for t in args.title:
        raw.append({"title": t, "year": args.year if len(args.title) == 1 else None,
                    "section": args.section, "search_type": "manual"})
    cands = [reg.normalize_candidate(c, args.section) for c in raw if isinstance(c, dict)]
    return [c for c in cands if c]


def run(args) -> int:
    try:
        sources = reg.parse_sources(args.sources)
        candidates = load_candidates(args)
    except (ValueError, FileNotFoundError, OSError) as e:
        return emit({"error": str(e)}, 1)
    if not candidates:
        return emit({"error": "no candidates: pass --candidates FILE or --title"}, 1)

    cutoff = args.cutoff or default_cutoff()
    started = datetime.datetime.now()
    registry = reg.Registry(args.out_dir)
    log(f"Verifying {len(candidates)} candidates via {'+'.join(sources)} (cutoff {cutoff})")
    outcomes = registry.verify(candidates, cutoff, sources)

    files = registry.save({
        "command": "verify",
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "cutoff": cutoff,
        "sources": list(sources),
        "candidates_file": os.path.abspath(args.candidates) if args.candidates else None,
        "candidates": outcomes,
    })
    result = {"out_dir": registry.out_dir, "cutoff": cutoff, "sources": list(sources)}
    result.update(reg.summarize(outcomes, registry))
    result["files"] = files
    return emit(result)
