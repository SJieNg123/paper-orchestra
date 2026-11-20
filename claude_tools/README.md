# PaperOrchestra tools for Claude Code

Tools that let Claude Code use PaperOrchestra's literature verification and
AgentReview reviewer **without any LLM API key**. Claude Code does the language
work itself (WebSearch for papers, Opus subagents for reviewing). These
commands do the parts that must be deterministic.

| Command | LLM? | What it does |
|---|---|---|
| `po verify` | none | Verifies candidate papers on Semantic Scholar and/or arXiv, applies a cutoff date, maintains `papers.json`, `references.bib`, `citation_map.json` |
| `po review-prep` | none | Extracts paper text, writes AgentReview reviewer instructions (and page images for a layout check), lists the subagents to spawn |
| `po review-aggregate` | none | Validates subagent reviews, prepares the area-chair meta-review, computes final scores with the upstream rule |
| `po lit-review` | Gemini API | Legacy: Gemini + Google Search proposes candidates, then the same verification |
| `po review` | Gemini API | Legacy: AgentReview run by Gemini |
| `po doctor` | none | Checks dependencies and API reachability |
| `po install-skill` | none | Installs the Claude Code skill |

Reviewer persona, review form, meta-review prompt and score aggregation come
from `autoraters/agent_review.py`. The layout-check prompt comes from
`methods/agents/content_refinement_agent.py`. Upstream code is not modified.

## Setup

```bash
cd paper-orchestra
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
claude_tools/po doctor
```

`po` uses the repo's `.venv` Python if present, otherwise `python3` / `python`,
or whatever `PAPER_ORCHESTRA_PYTHON` points to.

### Windows

Claude Code on Windows runs shell commands in Git Bash, which runs `po` as is.
From PowerShell or cmd, use `claude_tools\po.cmd` instead. Setup in PowerShell:

```powershell
cd paper-orchestra
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
claude_tools\po.cmd doctor
claude_tools\po.cmd install-skill --project C:\path\to\your\project
```

`.gitattributes` keeps `claude_tools/po` on LF line endings. If you cloned before
that file existed and `po` fails in Git Bash with `$'\r': command not found`,
re-checkout it: `del claude_tools\po` then `git checkout -- claude_tools/po`.
Output is always UTF-8, regardless of the console code page.

**Semantic Scholar key (recommended).** Without one, requests share a heavily
rate-limited pool and verification falls back to arXiv more often. Either:
- put `SEMANTIC_SCHOLAR_API_KEY=...` in `paper-orchestra/.env` (local machine), or
- in a Claude Code cloud environment, save it as a network secret for host
  `api.semanticscholar.org` with header `x-api-key`. The proxy adds it to every
  request, so no environment variable is needed.

arXiv needs no key. Gemini keys are only needed for the legacy commands.

## Use it from your research project

```bash
/path/to/paper-orchestra/claude_tools/po install-skill --project /path/to/your/project
# or for every project:
/path/to/paper-orchestra/claude_tools/po install-skill --user
```

This writes `.claude/skills/paper-orchestra/SKILL.md` with the absolute path to
`po` filled in. See [skill/paper-orchestra/SKILL.md](skill/paper-orchestra/SKILL.md)
for the full workflow.

## Workflow in short

Citations:
1. Claude Code plans explore and targeted searches and runs them with WebSearch.
2. It writes candidates (exact titles, year, section, source URL) to a JSON file.
3. `po verify --candidates c.json --out-dir paper/lit --cutoff 2026-11`
4. It cites only keys from `paper/lit/citation_map.json`.

Review:
1. `po review-prep --paper main.pdf --out-dir paper/reviews/v1`
2. Claude Code spawns the listed Opus subagents in parallel (each sees only the paper).
3. `po review-aggregate --dir paper/reviews/v1`, then follow `next_steps`
   (meta-review subagent) until `status` is `done`.

## Verification rules

- Fuzzy title match (ratio > 70, +10 for matching year), as upstream.
- Sources are tried in order (`--sources s2,arxiv`). Final status priority:
  `verified` > `after_cutoff` > `lookup_error` (retry later) > `not_found`.
- Semantic Scholar 429s are retried with backoff. arXiv is throttled to one
  request per 3 seconds as arXiv asks.
- State accumulates in `<out_dir>/papers.json`. Existing citation keys never
  change. Each run writes `runs/<time>.json` with every candidate's outcome.
- BibTeX escapes `& % # _`, keeps title capitalization, adds `doi` / `eprint`, and
  uses `arXiv preprint arXiv:<id>` for arXiv-only papers.

## Caveat on Claude-native reviews

Reviewers and author are the same model family, so reviews may be more lenient
on Claude-written text than an independent model would be. For a cross-model
check, run the legacy `po review` with a Gemini key and compare.

## Tests

```bash
.venv/bin/python -m pytest claude_tools/tests -q
```

Offline: Semantic Scholar, arXiv and Gemini are mocked.
