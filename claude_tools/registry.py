"""Persistent citation registry shared by `po verify` and `po lit-review`.

State lives in <out_dir>/papers.json. Every run regenerates references.bib and
citation_map.json from it and writes runs/<time>.json with per-candidate
outcomes. Citation keys are assigned once and never change.

Verification tries the configured sources in order (default: Semantic Scholar,
then arXiv). Final status priority:
  verified > after_cutoff > lookup_error > not_found
so a temporary API failure is reported as retryable instead of "not_found".
"""

import datetime
import os
from typing import Dict, List, Optional, Tuple

from claude_tools import arxiv, bib, s2
from claude_tools._common import log, read_json, write_json, write_text

SOURCES = {"s2": s2, "arxiv": arxiv}  # modules exposing search_title()
DEFAULT_SOURCES = ("s2", "arxiv")


def parse_sources(text: str) -> Tuple[str, ...]:
    names = tuple(s.strip().lower() for s in (text or "").split(",") if s.strip())
    bad = [n for n in names if n not in SOURCES]
    if bad or not names:
        raise ValueError(f"unknown source(s) {bad or text!r}, choose from {sorted(SOURCES)}")
    return names


def _truncate(abstract: str) -> str:
    abstract = abstract or ""
    return abstract[:1500] + "... [Truncated]" if len(abstract) > 1500 else abstract


def record_from_s2(p: Dict) -> Dict:
    ext = p.get("externalIds") or {}
    journal = p.get("journal") or {}
    return {
        "title": p["title"],
        "authors": [a.get("name", "") for a in p.get("authors") or [] if a.get("name")],
        "venue": p.get("venue") or "",
        "year": p.get("year"),
        "publication_date": p.get("publicationDate"),
        "abstract": _truncate(p.get("abstract")),
        "citation_count": p.get("citationCount") or 0,
        "journal": journal.get("name"),
        "volume": journal.get("volume"),
        "pages": (journal.get("pages") or "").strip() or None,
        "doi": ext.get("DOI"),
        "arxiv_id": ext.get("ArXiv"),
        "s2_paper_id": p.get("paperId"),
        "url": p.get("url"),
    }


def record_from_arxiv(p: Dict) -> Dict:
    return {
        "title": p["title"],
        "authors": p.get("authors") or [],
        "venue": p.get("journal_ref") or "arXiv",
        "year": p.get("year"),
        "publication_date": p.get("published"),
        "abstract": _truncate(p.get("abstract")),
        "citation_count": None,
        "journal": None,
        "volume": None,
        "pages": None,
        "doi": p.get("doi"),
        "arxiv_id": p.get("arxiv_id"),
        "s2_paper_id": None,
        "url": p.get("url"),
    }


_TO_RECORD = {"s2": record_from_s2, "arxiv": record_from_arxiv}


def verify_title(title: str, year: Optional[int], cutoff: str, sources=DEFAULT_SOURCES) -> Dict:
    """Return {"status", "record"?, "via"?, "matched_title"?, "matched_date"?, "errors"?}."""
    after, errors = None, {}
    for name in sources:
        status, paper = SOURCES[name].search_title(title, year, cutoff)
        if status == "verified":
            return {"status": "verified", "record": _TO_RECORD[name](paper), "via": name}
        if status == "after_cutoff" and after is None and paper:
            after = {"status": "after_cutoff", "via": name, "matched_title": paper.get("title"),
                     "matched_date": paper.get("publicationDate") or paper.get("published")
                     or paper.get("year")}
        elif status.endswith("_error"):
            errors[name] = (paper or {}).get("error", "error")
    if after:
        return after
    if errors:
        return {"status": "lookup_error", "errors": errors}
    return {"status": "not_found"}


def _cand_year(value) -> Optional[int]:
    try:
        return int(str(value)[:4]) if value else None
    except (TypeError, ValueError):
        return None


def normalize_candidate(c: Dict, default_section: str = "Related Work") -> Optional[Dict]:
    title = (c.get("title") or "").strip()
    if not title:
        return None
    return {
        "title": title,
        "year": _cand_year(c.get("year")),
        "reason": c.get("reason") or "",
        "section": c.get("section") or default_section,
        "query": c.get("query") or c.get("focus") or "",
        "search_type": c.get("search_type") or c.get("type") or "",
        "source_url": c.get("source_url") or c.get("url") or None,
    }


class Registry:
    def __init__(self, out_dir: str):
        self.out_dir = os.path.abspath(out_dir)
        self.papers_path = os.path.join(self.out_dir, "papers.json")
        papers = read_json(self.papers_path, default=[]) or []
        self.by_title: Dict[str, Dict] = {bib.normalize_title(p["title"]): p for p in papers}
        self.size_before = len(self.by_title)

    def verify(self, candidates: List[Dict], cutoff: str, sources=DEFAULT_SOURCES) -> List[Dict]:
        outcomes = []
        checked: Dict[str, Tuple[str, Optional[str]]] = {}
        taken = {p["citation_key"] for p in self.by_title.values()}

        for i, cand in enumerate(candidates, 1):
            norm = bib.normalize_title(cand["title"])
            out = {"candidate_title": cand["title"], "candidate_year": cand["year"],
                   "section": cand["section"], "query": cand["query"]}

            if norm in checked:
                first_status, key = checked[norm]
                out.update(status="duplicate_candidate", first_status=first_status, citation_key=key)
            elif norm in self.by_title:
                key = self.by_title[norm]["citation_key"]
                out.update(status="already_in_registry", citation_key=key)
                checked[norm] = ("already_in_registry", key)
            else:
                res = verify_title(cand["title"], cand["year"], cutoff, sources)
                status, key = res["status"], None
                if status == "verified":
                    rec = res["record"]
                    final_norm = bib.normalize_title(rec["title"])
                    out["matched_title"] = rec["title"]
                    out["via"] = res["via"]
                    if final_norm in self.by_title:
                        status, key = "already_in_registry", self.by_title[final_norm]["citation_key"]
                    else:
                        key = bib.unique_key(bib.base_key(rec["authors"], rec["year"], rec["title"]), taken)
                        taken.add(key)
                        rec.update({
                            "citation_key": key,
                            "verified_via": res["via"],
                            "found_in_section": cand["section"],
                            "found_by_query": cand["query"],
                            "search_type": cand["search_type"],
                            "reason": cand["reason"],
                            "source_url": cand["source_url"],
                            "added_at": datetime.datetime.now().isoformat(timespec="seconds"),
                        })
                        self.by_title[final_norm] = rec
                        out["added"] = True
                else:
                    for k in ("via", "matched_title", "matched_date", "errors"):
                        if k in res:
                            out[k] = res[k]
                checked[norm] = (status, key)
                out.update(status=status, citation_key=key)

            if i % 10 == 0 or i == len(candidates):
                log(f"  verified {i}/{len(candidates)} candidates")
            outcomes.append(out)
        return outcomes

    @property
    def papers(self) -> List[Dict]:
        return sorted(self.by_title.values(), key=lambda p: p["citation_key"])

    def save(self, run_report: Dict) -> Dict[str, str]:
        papers = self.papers
        files = {
            "papers": self.papers_path,
            "references_bib": os.path.join(self.out_dir, "references.bib"),
            "citation_map": os.path.join(self.out_dir, "citation_map.json"),
        }
        write_json(files["papers"], papers)
        write_text(files["references_bib"], bib.render_bibtex(papers))
        write_json(files["citation_map"], bib.citation_map(papers))
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        files["run_report"] = os.path.join(self.out_dir, "runs", f"{stamp}.json")
        write_json(files["run_report"], run_report)
        return files


def summarize(outcomes: List[Dict], registry: Registry) -> Dict:
    counts: Dict[str, int] = {}
    for o in outcomes:
        counts[o["status"]] = counts.get(o["status"], 0) + 1
    added = [o for o in outcomes if o.get("added")]
    summary = {
        "n_candidates": len(outcomes),
        "candidate_status": counts,
        "n_added": len(added),
        "registry_size": {"before": registry.size_before, "after": len(registry.by_title)},
        "added": [{"key": o["citation_key"], "title": o["matched_title"], "section": o["section"],
                   "via": o["via"]} for o in added],
    }
    retry = [o["candidate_title"] for o in outcomes if o["status"] == "lookup_error"]
    if retry:
        summary["retry_later"] = retry
    not_found = [o["candidate_title"] for o in outcomes if o["status"] == "not_found"]
    if not_found:
        summary["not_found"] = not_found
    return summary
