# Running ROAR 2.0 on the work computer

Same code, no cloud. The machine needs Python 3.11+, outbound HTTPS (PubMed, journal sites, smtp.gmail.com)
and, for summaries, either an Anthropic API key or a copy of the analysis you produce elsewhere.

## 1. Install

```powershell
git clone https://github.com/<GITHUB_USER>/ROAR2.0.git
cd ROAR2.0
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # then edit .env
python -m pytest -q         # sanity check, no network needed
```

`.env` needs:

```
ROAR_SMTP_USER=you@gmail.com
ROAR_SMTP_PASSWORD=<16-character Gmail app password>
ROAR_FROM=you@gmail.com
ROAR_TO=you@gmail.com
ROAR_NCBI_EMAIL=you@gmail.com
ANTHROPIC_API_KEY=<only if you use --backend anthropic>
```

Create the Gmail app password yourself at https://myaccount.google.com/apppasswords (2-step verification
must be on). Never paste it anywhere except `.env` (which is git-ignored) or a GitHub secret.

## 2. Try it once by hand

```powershell
python -m roar verify-sources                 # every feed + query, prints counts
python -m roar fetch                          # digests/<Monday>/candidates.json
python -m roar analyze --backend anthropic    # or: --backend none  (keyword-only, no summaries)
python -m roar render                         # digest.html / .md / subject.txt
python -m roar send --transport file          # writes digest.eml you can open in Outlook to preview
python -m roar send --transport smtp          # actually emails it
```

`python -m roar run --backend anthropic --transport smtp` does all of that in one go.

Cost with the API backend: one call per week with ~150 candidates ≈ 60–80k input tokens and ~8k output.
At Sonnet pricing ($2 / $10 per million) that is roughly $0.25 a week; Haiku is a fifth of that. Set the
model in `config/settings.yaml` → `analysis.anthropic_model`.

## 3. Schedule it (Windows Task Scheduler)

1. Task Scheduler → *Create Task…* → name `ROAR 2.0 weekly`.
2. *Triggers*: Weekly, Monday, 06:00. Tick *Run task as soon as possible after a scheduled start is missed*.
3. *Actions*: Start a program
   - Program: `C:\path\to\ROAR2.0\.venv\Scripts\python.exe`
   - Arguments: `-m roar run --backend anthropic --transport smtp`
   - Start in: `C:\path\to\ROAR2.0`
4. *Conditions*: untick *Start only if on AC power* if it is a laptop; tick *Wake the computer* if it sleeps.
5. *Settings*: *Stop the task if it runs longer than* 1 hour.

Logs: add `>> roar.log 2>&1` to the arguments through a small `run.cmd` wrapper if you want a log file:

```
@echo off
cd /d C:\path\to\ROAR2.0
.venv\Scripts\python.exe -m roar run --backend anthropic --transport smtp >> roar.log 2>&1
git add digests state && git commit -q -m "ROAR: digest" && git push -q
```

The last line keeps the GitHub copy current (needs git credentials on the machine; skip it otherwise).

## 4. macOS / Linux (cron)

```
0 6 * * 1  cd /path/to/ROAR2.0 && .venv/bin/python -m roar run --backend anthropic --transport smtp >> roar.log 2>&1
```

## 5. Hospital IT notes

- Outbound needs: `eutils.ncbi.nlm.nih.gov`, the feed hosts in `config/sources.yaml`, `smtp.gmail.com:587`,
  `api.anthropic.com` (only with the API backend). If SMTP is blocked, run with `--transport file` and let the
  GitHub/Cowork path do the sending instead.
- Nothing in the pipeline touches PHI; it only reads public feeds and writes files in the repo folder.
- If Python cannot be installed, the GitHub Actions + Cowork route (README) needs no local software at all.
