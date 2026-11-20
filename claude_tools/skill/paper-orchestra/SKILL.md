---
name: paper-orchestra
description: Verified citations and independent peer review for writing a research paper, with no extra API keys for LLMs. Use when drafting or revising a paper and you need real, verified papers to cite (Introduction, Related Work, or canonical citations for datasets, models, metrics, optimizers), need a references.bib, or want an AgentReview-style review with scores and weaknesses on a draft.
---

# PaperOrchestra tools (Claude-native)

You (Claude Code) do all the thinking: you search the web for papers and you run
reviewer subagents. The `po` tool only does what must not be left to a language
model: checking that papers exist (Semantic Scholar + arXiv), building BibTeX,
extracting PDF text, preparing reviewer instructions and aggregating scores.

Command: `{{PO}}`. Every call prints logs on stderr and exactly one JSON object on
stdout. Exit code 1 means the JSON has `"error"` (or an incomplete `"status"` with
`next_steps`). Run `{{PO}} doctor` if anything fails.

## Rules that always apply

- Cite ONLY keys present in `<lit_dir>/citation_map.json`. Never write a BibTeX
  entry or a citation key yourself, and never cite from memory.
- `references.bib` in the lit dir is regenerated on every run. Point the paper at
  it (or copy it after each run). Do not hand-edit it.
- Citation keys never change once assigned, so existing `\cite{}` stay valid.
- Every number in the paper comes from the user's experiment logs, never from a
  reviewer or from a cited abstract you have not checked.

## 1. Finding and verifying citations

### Step A: plan the searches

List the search tasks before searching:
- **explore** (aim for 10 to 15 relevant papers each): 3 to 5 for the
  Introduction (problem importance, surveys, foundational work), 1 to 3 for each
  Related Work cluster (closest competing methods, their known limitations).
- **targeted** (exactly one canonical paper each): one for EVERY dataset,
  baseline, metric, model architecture, optimizer, framework or prior result the
  paper mentions.

### Step B: search with WebSearch

For each task, use WebSearch (and WebFetch when a result page is needed) and
record candidates. Copy titles exactly from an authoritative page (arXiv,
publisher, venue proceedings, DBLP, Semantic Scholar), not from memory or a blog
paraphrase. With more than about 6 tasks, split them across a few
`general-purpose` subagents in parallel, each writing its own candidates file,
so the search results do not fill your context.

Candidates file (`title` required, the rest strongly recommended):

```json
{"candidates": [
  {"title": "Deep Learning with Differential Privacy", "year": 2016,
   "section": "Related Work: Private training", "query": "DP-SGD original paper",
   "reason": "Introduces DP-SGD, our main baseline", "source_url": "https://arxiv.org/abs/1607.00133"}
]}
```

Use the same `section` label for papers that belong together. It is stored with
each paper and helps you place citations later.

### Step C: verify

```bash
{{PO}} verify --candidates paper/lit/cands_intro.json --out-dir paper/lit --cutoff 2026-11
```

Single title: `{{PO}} verify --out-dir paper/lit --title "Exact Title" --year 2023 --section Experiments`.

`--cutoff YYYY-MM` excludes papers published on or after that month (default:
current month, set it to the submission date for a fair Related Work).
`--sources s2,arxiv` is the default lookup order. Expect about 1 to 4 seconds per
candidate, more when Semantic Scholar is rate limited. Run large batches with
`run_in_background`.

### Step D: read the result

- `added`: new keys. Read the abstracts in `citation_map.json` before citing, and
  cite each paper only for what its abstract or text supports.
- `not_found`: no matching title in Semantic Scholar or arXiv. Usually a wrong
  or invented title. Do not cite it. If you are sure it exists, find the exact
  title with WebSearch and verify again.
- `retry_later` (`lookup_error`): an API was unavailable. Rerun `verify` on the
  same file later. Already verified papers are skipped automatically.
- `after_cutoff`: published after the cutoff. Mention it only as concurrent work,
  if at all.
- `via`: which source verified it. arXiv-only papers get an arXiv preprint entry.
  If a peer-reviewed version exists, verify its exact published title too.
- Spot-check year and venue of important citations in `papers.json`. Semantic
  Scholar metadata is sometimes off (e.g. arXiv year instead of conference year).

## 2. Peer review with Opus subagents

Reviewers must be fresh subagents that see only the paper, never this
conversation. Do not review the paper yourself in the main thread and do not edit
review files.

1. Prepare (a compiled PDF is best, `.tex` / `.md` also work):
   ```bash
   {{PO}} review-prep --paper paper/main.pdf --out-dir paper/reviews/v1
   ```
   Options: `--ensemble N` (default 3), `--no-benign` / `--no-knowledgeable` /
   `--no-responsible` for harsher or weaker personas, `--format-check --guidelines
   guidelines.md` to add a layout check on rendered pages.
2. The output's `next_steps.1_spawn_in_parallel` lists Agent calls. Make ALL of
   them in ONE message, copying `subagent_type`, `model` (opus) and `prompt`
   exactly. Add nothing about the paper or its authors to the prompt.
3. When they finish, run the `next_steps.2_then_run` command
   (`po review-aggregate --dir ...`). Its output either:
   - says `needs_meta_review`: spawn the one area-chair subagent it lists, then
     run aggregate again, or
   - says `incomplete_reviews` / `invalid_meta_review`: respawn exactly the
     listed subagents, then run aggregate again, or
   - says `done`: `final_review.json` is complete.

Using the review:
1. Report decision, scores and main weaknesses to the user. Say that reviewers
   are Claude subagents (same model family as the author), so scores may be
   somewhat lenient on Claude-written text.
2. Triage each weakness and question:
   - Presentation or clarity issue: fix the text.
   - Information that exists in the logs but is missing from the paper: add it.
   - Needs a new experiment or result: do NOT invent anything. List it for the
     user as a possible experiment.
3. After revising, recompile and review again into a NEW `--out-dir` with the
   same settings, and compare. `overall_spread` shows how much reviewers
   disagree: a change smaller than the spread is noise. Prefer substantive fixes
   over score chasing.

## Legacy Gemini commands

`po lit-review` (Gemini + Google Search) and `po review` (Gemini reviewer) still
exist and write to the same registry and review format. They need
`GEMINI_API_KEY`. Use them only when the user asks for Gemini, e.g. as a
cross-model comparison.
