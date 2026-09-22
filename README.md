# ROAR 2.0 — Radiation Oncology Aggregator Resource

A weekly, emailed literature digest for one radiation oncologist. It replaces a $144/year Feedly
subscription that surfaced <10 % relevant articles with a free pipeline that reads ~45 feeds and seven
PubMed queries, pre-filters ~500–800 items a week down to ~150 candidates, hands them to an analyst
(Claude) that picks the 10 most consequential plus ~30 more worth knowing, and emails an HTML digest every Monday morning
with links to the article, PubMed and the University of Michigan full-text proxy.

```
                 GitHub Actions (Sunday night)                 Cowork scheduled task (Monday 06:00 AKT)
 ┌──────────────────────────────────────────────┐    ┌───────────────────────────────────────────────┐
 │ RSS feeds ──┐                                │    │ git clone → read candidates + analyst prompt  │
 │             ├─ merge ─ enrich ─ pre-score ─► │    │ Claude ranks + writes summaries → analysis.json│
 │ PubMed ─────┘   seen-state    candidates.json│───►│ roar render → digest.html → Gmail send        │
 │            commit to repo                    │    │ (optional) commit digest back                  │
 └──────────────────────────────────────────────┘    └───────────────────────────────────────────────┘
                     ▲ same code runs end-to-end on a work PC with an API key: `roar run`
```

## Repository layout

| path | what |
|---|---|
| `config/settings.yaml` | recipient, lookback window, caps, section order, U-M proxy prefix, backends |
| `config/sources.yaml` | RSS/Atom feeds with tier, category, topic filter, enabled flag |
| `config/pubmed.yaml` | the seven PubMed queries, built from shared fragments |
| `config/interests.yaml` | **the reader profile** (embedded in the analyst prompt) + keyword scoring rules + disease-site terms |
| `config/conferences.yaml` | upcoming meetings for the "Coming up" footer |
| `roar/` | the pipeline (`fetch_rss`, `fetch_pubmed`, `normalize`, `enrich`, `prescore`, `state`, `analyze`, `render`, `send`, `cli`) |
| `roar/prompts/analyst.md` | analyst instructions (what to pick, how to write), shared by every backend |
| `roar/templates/` | Gmail-safe HTML email + Markdown templates |
| `digests/YYYY-MM-DD/` | one folder per digest: `candidates.json/.md`, `analyst_input.md`, `analyst_prompt.md`, `analysis.json`, `digest.html/.md/.txt`, `subject.txt`, `fetch_report.json` |
| `state/seen.json` | ids already shown (120-day memory) so nothing appears twice, plus the `pending` register of items held back until an abstract turns up |
| `.github/workflows/` | `weekly.yml` (fetch + commit every Monday 06:15 UTC) and `verify-sources.yml` (manual check) |
| `docs/` | `COWORK_TASK.md` (the scheduled-task prompt), `WORK_COMPUTER.md`, `SOURCES.md` |
| `tests/` | offline tests (`python -m pytest -q`) |

## Commands

```
python -m roar fetch            # RSS + PubMed → digests/<Monday>/candidates.json  (network)
python -m roar analyze          # --backend file | anthropic | none
python -m roar render           # digest.html / digest.md / digest.txt / subject.txt
python -m roar send             # --transport smtp | file
python -m roar run              # all four
python -m roar verify-sources   # hit every feed and query once, print counts
python -m roar show-prompt      # the analyst system prompt with the profile filled in
python -m roar latest           # path of the newest digest folder
```

Every command takes `--week-of YYYY-MM-DD` (the Monday the digest is delivered; default = this/next
Monday). `fetch` also takes `--from-items file.json` to run the pipeline on items collected elsewhere.

## How an item gets in

1. **Collect.** Feeds are parsed with `feedparser`; items older than `lookback_days` are dropped; broad
   feeds (NEJM, JAMA, Nature Medicine, FDA…) must match an oncology/RT keyword. PubMed runs the seven queries
   with `reldate=8 datetype=edat`, pulls `esummary` for everything, pre-scores on title/journal/pubtype, and
   fetches abstracts only for the best ~280. JCO and JCO Oncology Practice come from PubMed only (`jco_all`):
   their publisher feeds carry a citation line instead of an abstract, no date, and list papers months after
   online publication, whereas PubMed has them with abstracts the same day.
2. **Merge.** Records that share a PMID, DOI or normalised title are merged; PubMed wins as the base record
   (abstract, publication types), every source id is kept. Anything already in `state/seen.json` is dropped here.
3. **Enrich.** Journal items that are still a citation line ("…, Ahead of Print.", "Publication date: …
   Author(s): …", or a PubMed record below the stage-2 cut) are looked up: PubMed by PMID (batched), then
   Crossref by DOI (ASCO and most publishers deposit abstracts there; Elsevier does not; also supplies the
   online-publication date), then PubMed by DOI, then Semantic Scholar. Items without a DOI are first matched
   by exact title in Crossref. Budgeted (`enrich.max_lookups`, `time_budget_s`) and best-effort; `--no-enrich`
   turns it off.
4. **Pre-score.** `config/interests.yaml` rules: +3 randomised/phase 3, +3 guideline, +3 RT terms, journal
   tier bonus, emphasis-site bonus; −4 bench/case report, −3 protocols/letters, −2 low-yield
   journals. Disease sites are classified from the same file.
5. **Filter.** Journal items that still have no abstract are **deferred**: kept out of this week's list and
   out of `seen`, recorded in `pending`, so that next week's PubMed/Crossref record can claim them; after
   `enrich.defer_days` (14) they are shown as they are. If last week's folder was never rendered its
   candidates are carried over. Caps: `min_prescore`, `max_per_source`, `max_total`.
6. **Analyse.** The analyst gets `analyst_prompt.md` (instructions + profile) and `analyst_input.md`
   (compact candidate list) and returns `analysis.json`: top picks, section, score, headline, 2–3-sentence
   summary, why it matters, tags. `roar analyze` validates it (unknown ids dropped, sections coerced,
   ceilings enforced) and writes `analysis.validated.json`.
7. **Render + send.** Jinja2 → table-based inline-CSS HTML that renders in Gmail/Outlook, plus Markdown and
   plain text; subject line = week + lead headline. Links: Article (DOI/publisher), PubMed, U-M full text
   (`https://proxy.lib.umich.edu/login?url=…`).

## Three ways to run it

### A. GitHub Actions + Cowork (default, free) — live at https://github.com/mdgreen04/ROAR2.0
1. The repo is **public** on purpose: a Cowork cloud session has no GitHub credentials, so it can only
   clone a public repo. Nothing personal is committed — the recipient address comes from the task prompt
   (Cowork) or the `ROAR_TO` secret (Actions), the NCBI contact address from `ROAR_NCBI_EMAIL`, and the
   repo holds only article metadata and abstracts.
2. Actions → enable workflows. `ROAR weekly fetch` runs Mondays 06:15 UTC and commits candidates.
3. Make sure the Gmail connector in Claude is allowed to *send* (read-only Gmail cannot deliver the digest).
4. The Cowork scheduled task in `docs/COWORK_TASK.md` (Mondays 14:00 UTC = 06:00 AKDT, automatic
   approval) clones the repo, analyses, renders and emails via the Gmail connector.

### B. Fully autonomous in GitHub Actions
Add repository secrets `ANTHROPIC_API_KEY`, `ROAR_SMTP_USER`, `ROAR_SMTP_PASSWORD` (Gmail app password),
`ROAR_FROM`, `ROAR_TO`; run the workflow with `backend=anthropic`, `send=true` (or change the workflow defaults).
About $0.25/week at Sonnet prices.

### C. Work computer
`docs/WORK_COMPUTER.md` — venv, `.env`, Task Scheduler, `python -m roar run --backend anthropic --transport smtp`.

## Tuning

- **Interests / voice**: edit `profile` in `config/interests.yaml` — it is pasted into the analyst prompt
  verbatim. Add or remove sites, change emphasis, change what "skip" means.
- **Sources**: toggle `enabled`, change `tier`, add feeds. `python -m roar verify-sources` checks them.
- **Digest size**: `digest_shape` in `config/settings.yaml` (top picks, max items, tidbits per section).
- **Queries**: `config/pubmed.yaml`; test a change quickly at https://pubmed.ncbi.nlm.nih.gov with the same
  string (`python -m roar verify-sources` prints the count for each).
- **Abstract look-ups**: `enrich` in `config/settings.yaml` (categories, budget, deferral window). Set the
  `ROAR_CROSSREF_MAILTO` env/secret for Crossref's polite pool and `S2_API_KEY` for a steadier Semantic Scholar
  rate limit; both are optional.
- **Look**: `roar/templates/digest.html.j2` (inline styles only; no external CSS for email clients).

## Offline collection (when the runner cannot reach PubMed)

`tools/browser_collect.js` reproduces the PubMed and feed fetch inside an ordinary browser tab and saves
JSON downloads; `tools/items_from_browser_export.py` turns them into item files for
`python -m roar fetch --from-items …`. This is how the first sample digest (`digests/2026-09-15/`) was
produced from a sandbox that could not reach the journals.

## Requirements

Python 3.11+, `feedparser`, `requests`, `PyYAML`, `Jinja2`, `python-dateutil`; `anthropic` only for the API
backend. `pip install -r requirements.txt`. Tests: `python -m pytest -q` (no network needed).

## License

MIT — see `LICENSE`.
