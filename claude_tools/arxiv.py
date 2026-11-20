"""arXiv title verification (no API key needed).

Used alongside Semantic Scholar: same fuzzy-title rule (ratio > 70, +10 for a
matching year) and the same cutoff semantics, where the arXiv date is the
submission date of the first version.
"""

import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

import requests
from thefuzz import fuzz  # type: ignore

from claude_tools.s2 import MATCH_THRESHOLD, _before_cutoff

ARXIV_API = "https://export.arxiv.org/api/query"
_NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

# arXiv asks API clients for at most one request every 3 seconds.
_MIN_INTERVAL_SEC = 3.0
_last_call = [0.0]
_lock = threading.Lock()


def _throttle():
    with _lock:
        wait = _MIN_INTERVAL_SEC - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_feed(xml_text: str) -> List[Dict]:
    root = ET.fromstring(xml_text)
    out = []
    for e in root.findall("a:entry", _NS):
        abs_id = e.findtext("a:id", default="", namespaces=_NS)
        m = re.search(r"abs/([^v\s]+)(v\d+)?$", abs_id)
        if not m:
            continue
        published = (e.findtext("a:published", default="", namespaces=_NS) or "")[:10]
        out.append({
            "arxiv_id": m.group(1),
            "title": _clean(e.findtext("a:title", default="", namespaces=_NS)),
            "abstract": _clean(e.findtext("a:summary", default="", namespaces=_NS)),
            "authors": [_clean(a.findtext("a:name", default="", namespaces=_NS))
                        for a in e.findall("a:author", _NS)],
            "published": published or None,
            "year": int(published[:4]) if published[:4].isdigit() else None,
            "journal_ref": _clean(e.findtext("arxiv:journal_ref", default="", namespaces=_NS)) or None,
            "doi": _clean(e.findtext("arxiv:doi", default="", namespaces=_NS)) or None,
            "url": f"https://arxiv.org/abs/{m.group(1)}",
        })
    return out


def _queries(title: str) -> List[str]:
    words = re.sub(r"[^A-Za-z0-9\s]", " ", title).split()
    if not words:
        return []
    phrase = " ".join(words)
    long_words = [w for w in words if len(w) > 3][:8] or words[:8]
    return [f'ti:"{phrase}"', " AND ".join(f"ti:{w}" for w in long_words)]


def _fetch(query: str, max_retries: int, timeout: int) -> Tuple[Optional[List[Dict]], str]:
    last_err = ""
    for attempt in range(max_retries):
        _throttle()
        try:
            rsp = requests.get(ARXIV_API, params={"search_query": query, "max_results": 5},
                               timeout=timeout)
            if rsp.status_code == 200:
                return parse_feed(rsp.text), ""
            last_err = f"HTTP {rsp.status_code}"
            if rsp.status_code not in (429, 500, 502, 503, 504):
                break
        except (requests.RequestException, ET.ParseError) as e:
            last_err = str(e)
        time.sleep(min(3 * 2 ** attempt, 30))
    return None, last_err


def search_title(
    title: str,
    year_hint: Optional[int] = None,
    cutoff: Optional[str] = None,
    max_retries: int = 4,
    timeout: int = 20,
) -> Tuple[str, Optional[dict]]:
    """Same contract as s2.search_title: (verified|after_cutoff|not_found|arxiv_error, paper)."""
    any_ok, last_err = False, ""
    best, best_ratio, best_any, best_any_ratio = None, 0, None, 0

    for q in _queries(title):
        results, err = _fetch(q, max_retries, timeout)
        if results is None:
            last_err = err
            continue
        any_ok = True
        for r in results:
            ratio = fuzz.ratio(title.lower(), r["title"].lower())
            if year_hint and r.get("year") == year_hint:
                ratio += 10
            if ratio > best_any_ratio:
                best_any, best_any_ratio = r, ratio
            if _before_cutoff(r.get("published"), r.get("year"), cutoff) and ratio > best_ratio:
                best, best_ratio = r, ratio
        if best is not None and best_ratio > MATCH_THRESHOLD:
            return "verified", best

    if not any_ok:
        return "arxiv_error", {"error": last_err}
    if best_any is not None and best_any_ratio > MATCH_THRESHOLD:
        return "after_cutoff", best_any
    return "not_found", None
