---
name: paper-orchestra
description: Verified literature search (citations checked against Semantic Scholar, BibTeX generated) and independent Gemini peer review (AgentReview ensemble with scores) for writing a research paper. Use when drafting or revising a paper and you need real citations for an Introduction / Related Work / Experiments section, need a canonical citation for a dataset, model, metric or optimizer, or want an external reviewer's scores and weaknesses on a compiled draft.
---

# PaperOrchestra tools

You (Claude Code) are the author and orchestrator. These tools cover two things
you should not do from memory: finding real, verified papers to cite, and getting
a review from a different model family. Writing, LaTeX editing and compiling stay
with you.

Command: `{{PO}}`. Each call prints logs to stderr and exactly one JSON object to
stdout. Exit code 1 means the JSON has an `"error"` field. Run
`{{PO}} doctor` first if anything fails.

## Rules that always apply

- Cite ONLY keys present in `<lit_dir>/citation_map.json`. Never invent a BibTeX
  entry or a key. If a needed citation is missing, run a `--find` lookup.
- `references.bib` in the lit dir is regenerated on every run. Point the paper at
  it, or copy it after each run. Do not hand-edit it.
- Citation keys are stable across runs, so already-written `\cite{}` stay valid.
- Every number in the paper must come from the user's experiment logs, never from
  a reviewer suggestion.

## 1. Literature search: `lit-review`

Takes 2 to 15 minutes depending on the number of tasks. Run it with
`run_in_background` and keep working, or tell the user it is running.

Write a plan file first (simple format):

```json
{
  "problem": "One sentence: the gap this paper addresses",
  "tasks": [
    {"section": "Introduction", "type": "explore", "query": "surveys on referring audio-visual segmentation"},
    {"section": "Related Work: Video foundation models", "type": "explore",
     "query": "temporal extensions of Segment Anything for video",
     "context": "We need methods that add temporal modeling to SAM"},
    {"section": "Experiments", "type": "targeted", "query": "research paper introducing 'Ref-AVS dataset'"},
    {"section": "Experiments", "type": "targeted", "query": "Loshchilov and Hutter (Decoupled Weight Decay Regularization)"}
  ]
}
```

- `explore`: returns ~10 to 15 related papers. Use 3 to 5 for the Introduction
  (context, surveys, foundational work) and 1 to 3 per Related Work cluster.
- `targeted`: finds exactly one canonical paper. Add one for EVERY dataset,
  baseline, metric, architecture and optimizer the paper mentions. Use
  `Author (Exact Title)` when known, otherwise
  `research paper introducing '<Name>'`.
- An upstream PaperOrchestra `outline.json` is also accepted as `--plan`.

```bash
{{PO}} lit-review --plan paper/lit_plan.json --out-dir paper/lit --cutoff 2025-11
```

Quick follow-up for a single missing citation (same `--out-dir` accumulates):

```bash
{{PO}} lit-review --out-dir paper/lit --section Experiments --find "research paper introducing 'DAVIS 2017'"
```

Options: `--cutoff YYYY-MM` excludes papers published on or after that month
(default: current month, set it to the submission date). `--dry-run` shows the
parsed tasks without calling any API. `--write-draft --template T --idea I
--experimental-log L` also produces Gemini's own Intro + Related Work draft
(`intro_related_work_draft.tex`). Treat it only as a comparison baseline.

Reading the result:
- `added`: new papers with their keys. Read `citation_map.json` for abstracts
  before citing, and cite each paper for what its abstract actually supports.
- `candidate_status`: `not_found` means Gemini proposed a title Semantic Scholar
  could not match (often hallucinated, never cite it). `after_cutoff` means too
  recent. `s2_error` means rate limited, so rerun the same command later (already
  verified papers are skipped).
- `failed_tasks`: Gemini search errors. Rerun or rephrase.
- Per-candidate details are in `runs/<timestamp>.json`.

## 2. External review: `review`

Needs a draft. A compiled PDF is best because that is what the reviewer was
designed for. `.tex` or `.md` also work. Takes 1 to 5 minutes.

```bash
{{PO}} review --paper paper/main.pdf --out paper/reviews/v1.json
```

Options: `--ensemble N` (default 3 reviews plus an area-chair meta-review),
`--model`, `--no-benign` / `--no-knowledgeable` / `--no-responsible` for harsher
or weaker reviewer personas, `--format-check --guidelines guidelines.md` for a
VLM layout check of the rendered pages (figure overflow, margins).

Using the review:
1. Report the scores and the main weaknesses to the user.
2. Triage each weakness and question:
   - Presentation or clarity issue: fix it in the text.
   - Information that exists in the logs but is missing from the paper: add it.
   - Needs a new experiment or result: do NOT invent anything. List it for the
     user as a possible experiment.
3. After revising, recompile and review again with the same settings and compare.
   `overall_spread` shows how much reviews disagree. A change smaller than the
   spread is noise, so do not chase it. Prefer substantive fixes over score
   chasing.
