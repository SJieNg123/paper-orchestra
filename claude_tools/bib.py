"""Citation key assignment and BibTeX rendering for the paper registry."""

import re
from typing import Dict, Iterable, List

_STOPWORDS = {
    "the", "a", "an", "in", "on", "at", "for", "to", "of", "and",
    "is", "are", "with", "by", "study",
}


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (title or "").lower())


def base_key(authors: List[str], year, title: str) -> str:
    """Same key scheme as upstream HybridLiteratureAgent._generate_key."""
    first = authors[0].split()[-1] if authors and authors[0].split() else "Unknown"
    author = re.sub(r"[^a-zA-Z]", "", first).capitalize() or "Unknown"
    year_str = str(year) if year else "nd"
    words = re.sub(r"[^a-zA-Z0-9\s]", "", (title or "").lower()).split()
    meaningful = [w.capitalize() for w in words if w not in _STOPWORDS]
    return f"{author}{year_str}{''.join(meaningful[:2]) or 'Paper'}"


def unique_key(key: str, taken: Iterable[str]) -> str:
    taken = set(taken)
    if key not in taken:
        return key
    for suffix in "abcdefghijklmnopqrstuvwxyz":
        if key + suffix not in taken:
            return key + suffix
    i = 2
    while f"{key}{i}" in taken:
        i += 1
    return f"{key}{i}"


_ESCAPES = [("&", r"\&"), ("%", r"\%"), ("#", r"\#"), ("_", r"\_")]


def _escape(text: str) -> str:
    text = str(text or "")
    for ch, rep in _ESCAPES:
        text = re.sub(r"(?<!\\)" + re.escape(ch), lambda _m, r=rep: r, text)
    return text.replace("\n", " ").strip()


def _is_arxiv_only(p: Dict) -> bool:
    venue = (p.get("venue") or "").strip().lower()
    return bool(p.get("arxiv_id")) and venue in ("", "arxiv", "arxiv.org", "corr")


def render_entry(p: Dict) -> str:
    fields = [("title", "{" + _escape(p["title"]) + "}")]
    fields.append(("author", " and ".join(_escape(a) for a in p.get("authors") or []) or "Unknown"))

    if _is_arxiv_only(p):
        entry_type = "article"
        fields.append(("journal", f"arXiv preprint arXiv:{p['arxiv_id']}"))
    elif p.get("journal"):
        entry_type = "article"
        fields.append(("journal", _escape(p["journal"])))
    else:
        entry_type = "inproceedings"
        fields.append(("booktitle", _escape(p.get("venue") or "arXiv")))

    if p.get("year"):
        fields.append(("year", str(p["year"])))
    for name in ("volume", "pages"):
        if p.get(name):
            fields.append((name, _escape(p[name])))
    if p.get("doi"):
        fields.append(("doi", p["doi"]))
    if p.get("arxiv_id"):
        fields.append(("eprint", p["arxiv_id"]))
        fields.append(("archivePrefix", "arXiv"))

    body = ",\n".join(f"  {k}={{{v}}}" for k, v in fields)
    return f"@{entry_type}{{{p['citation_key']},\n{body}\n}}"


def render_bibtex(papers: List[Dict]) -> str:
    return "\n\n".join(render_entry(p) for p in papers) + ("\n" if papers else "")


def citation_map(papers: List[Dict]) -> Dict[str, Dict]:
    """Same shape upstream agents consume (citation_map.json)."""
    return {
        p["citation_key"]: {
            "citation_key": p["citation_key"],
            "title": p["title"],
            "authors": p.get("authors") or [],
            "venue": p.get("venue") or "",
            "year": p.get("year"),
            "abstract": (p.get("abstract") or "").strip(),
        }
        for p in papers
    }
