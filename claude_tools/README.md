# PaperOrchestra tools for Claude Code

Two PaperOrchestra components exposed as command-line tools, so Claude Code can
stay the author and orchestrator and call them only where they add something it
cannot do itself:

| Command | Wraps | What it gives Claude Code |
|---|---|---|
| `po lit-review` | `methods/agents/literature_review_agent.py` | Gemini + Google Search proposes papers, Semantic Scholar verifies each one, cutoff-date filter, `references.bib` + `citation_map.json` |
| `po review` | `autoraters/agent_review.py` (+ formatting check from `content_refinement_agent.py`) | Independent reviewer from another model family: AgentReview ensemble, meta-review, scores, weaknesses, questions |

Upstream code is not modified. The prompts are the upstream prompts.

## Setup

```bash
cd paper-orchestra
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# put GEMINI_API_KEY / SEMANTIC_SCHOLAR_API_KEY in .env (see the main README)
claude_tools/po doctor
```

Required: `GEMINI_API_KEY` (or `VERTEX_AI_PROJECT` + `VERTEX_AI_LOCATION`).
Strongly recommended: `SEMANTIC_SCHOLAR_API_KEY`. Without it, verification uses
a shared, heavily rate-limited pool. The tool retries 429s and reports
leftovers as `s2_error`, and rerunning the same command skips papers that are
already verified.

`po` uses `.venv/bin/python` if present, otherwise `python3`, or whatever
`PAPER_ORCHESTRA_PYTHON` points to (e.g. a conda env's python).

## Use it from your research project

```bash
/path/to/paper-orchestra/claude_tools/po install-skill --project /path/to/your/project
# or for every project:
/path/to/paper-orchestra/claude_tools/po install-skill --user
```

This writes `.claude/skills/paper-orchestra/SKILL.md` with the absolute path to
`po` filled in. Claude Code then knows when and how to call the tools. See
[skill/paper-orchestra/SKILL.md](skill/paper-orchestra/SKILL.md) for the full
usage guide (plan format, options, how to read results).

## Output contract

- Logs on stderr, one JSON object on stdout, exit code 1 on handled failure.
- `lit-review --out-dir D` keeps state in `D/papers.json` and accumulates across
  calls. Existing citation keys never change. `D/references.bib` and
  `D/citation_map.json` are regenerated each run. `D/runs/<time>.json` records
  every task and every candidate with its verification status.
- `review` saves the meta-review, all individual reviews and per-axis score
  mean, std and values to one JSON file.

## Differences from the upstream pipeline

- Semantic Scholar HTTP 429 is retried instead of being treated as "not found",
  and every dropped candidate gets a reason (`not_found`, `after_cutoff`,
  `s2_error`, duplicates).
- Papers without an abstract on Semantic Scholar are kept (upstream drops them),
  since canonical dataset and optimizer papers sometimes lack one.
- BibTeX escapes `& % # _`, keeps title capitalization, and adds `doi` /
  `eprint` when available.
- The search model is configurable (`--search-model`, upstream hard-codes
  `gemini-3-flash-preview`).
- Ensemble reviews run in parallel and are all saved. The final scores follow
  the upstream rule (rounded mean of individual reviews).

## Tests

```bash
.venv/bin/python -m pytest claude_tools/tests -q
```

Offline: Gemini and Semantic Scholar are mocked.
