"""Semantic Scholar title verification.

Same matching rule as upstream `utils/scholar_utils.s2_title_search` (fuzzy
title ratio > 70, +10 bonus for matching year, cutoff-date filter), but it:
  * retries on HTTP 429 instead of silently treating it as "not found",
  * returns a status so callers can report *why* a candidate was dropped,
  * never prints the API key.
"""

import datetime
import os
import threading
import time
from typing import Optional, Tuple

import requests
from thefuzz import fuzz  # type: ignore

S2_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
S2_FIELDS = (
    "title,authors,venue,year,abstract,citationCount,journal,"
    "publicationDate,externalIds,url,paperId"
)
MATCH_THRESHOLD = 70

# Unauthenticated traffic shares a global pool and gets 429s often.
_MIN_INTERVAL_SEC = 1.05
_last_call = [0.0]
_lock = threading.Lock()


def _throttle():
    with _lock:
        wait = _MIN_INTERVAL_SEC - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()


def _before_cutoff(p_date: Optional[str], p_year: Optional[int], cutoff: Optional[str]) -> bool:
    """True if the paper was published before the YYYY-MM cutoff (same rule as upstream)."""
    if not cutoff:
        return True
    try:
        c_year, c_month = map(int, cutoff.split("-")[:2])
        cutoff_dt = datetime.datetime(c_year, c_month, 1)
        if p_date:
            try:
                return datetime.datetime.strptime(p_date, "%Y-%m-%d") < cutoff_dt
            except ValueError:
                pass
        if p_year:
            if p_year < c_year:
                return True
            return p_year == c_year and c_month > 1
        return True
    except Exception:
        return True


def search_title(
    title: str,
    year_hint: Optional[int] = None,
    cutoff: Optional[str] = None,
    max_retries: int = 4,
    timeout: int = 15,
) -> Tuple[str, Optional[dict]]:
    """Look up a candidate title on Semantic Scholar.

    Returns (status, paper) where status is one of:
      verified       paper matched and is before the cutoff
      after_cutoff   best title match exists but was published after the cutoff
      not_found      no sufficiently similar title
      s2_error       network / rate limit problems persisted after retries
    """
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    headers = {"x-api-key": api_key} if api_key else {}
    params = {"query": title, "limit": 3, "fields": S2_FIELDS}

    results = None
    last_err = ""
    for attempt in range(max_retries):
        _throttle()
        try:
            rsp = requests.get(S2_SEARCH_URL, headers=headers, params=params, timeout=timeout)
        except requests.RequestException as e:
            last_err = str(e)
        else:
            if rsp.status_code == 200:
                results = rsp.json().get("data", []) or []
                break
            last_err = f"HTTP {rsp.status_code}"
            if rsp.status_code not in (429, 500, 502, 503, 504):
                break
        time.sleep(min(2 ** (attempt + 1), 30))

    if results is None:
        return "s2_error", {"error": last_err}

    best, best_ratio = None, 0
    best_any, best_any_ratio = None, 0
    for r in results:
        if not r.get("title"):
            continue
        ratio = fuzz.ratio(title.lower(), r["title"].lower())
        if year_hint and r.get("year") == year_hint:
            ratio += 10
        if ratio > best_any_ratio:
            best_any, best_any_ratio = r, ratio
        if _before_cutoff(r.get("publicationDate"), r.get("year"), cutoff) and ratio > best_ratio:
            best, best_ratio = r, ratio

    if best is not None and best_ratio > MATCH_THRESHOLD:
        return "verified", best
    if best_any is not None and best_any_ratio > MATCH_THRESHOLD:
        return "after_cutoff", best_any
    return "not_found", None
