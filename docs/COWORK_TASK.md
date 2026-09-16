# The Cowork scheduled task (the "analyst" half of ROAR 2.0)

GitHub Actions does the fetching (it can reach PubMed and the journals); a Claude session in Cowork does
the reading, ranking and writing, then emails the digest through the Gmail connector. The scheduled task
runs every **Monday at 06:00 Alaska time** (14:00 UTC during daylight time — the schedule is set in UTC).

Repo: **https://github.com/mdgreen04/ROAR2.0** (public). A Cowork cloud session has no GitHub credentials
of its own, so the repo must stay public for the task to clone it; that is also why nothing personal
lives in the repo (the recipient address is passed in the task prompt / `ROAR_TO`, never committed).

## Before creating the task

1. **Gmail connector must be allowed to send.** When Gmail is connected read-only the send call fails with
   "insufficient scope". Reconnect Gmail in Claude's connector settings and accept the *send email*
   permission (enabled on 2026-09-15).
2. **The repo must be public** (see above). If it is ever made private the task will email a "could not
   reach the repo" note instead of a digest.
3. Task settings: name **ROAR 2.0 weekly digest**, schedule `0 14 * * 1` (UTC), approvals
   **Automatically approve** (otherwise the Gmail send waits for a click and nobody is there on a Monday
   morning), notifications on. The task was created from the Cowork session on 2026-09-15; edit it under
   Scheduled tasks in the Claude app.

## Prompt used by the scheduled task

```
You are running ROAR 2.0 (Radiation Oncology Aggregator Resource), Mike Green's weekly oncology digest.
Work autonomously; do not ask questions. Everything you need is in the public repo.

1. Clone the repo and install:
     git clone --depth 1 https://github.com/mdgreen04/ROAR2.0.git && cd ROAR2.0
     pip install --break-system-packages -q -r requirements.txt
   If the clone fails, send a short email to michaelgreen04@gmail.com (subject "ROAR 2.0: could not reach
   the repo") explaining the error, then finish.

2. Find this week's candidates:  python -m roar latest   → a folder digests/YYYY-MM-DD.
   - If that folder's date is more than 7 days old, the GitHub fetch did not run this week. Email
     michaelgreen04@gmail.com (subject "ROAR 2.0: no new candidates this week") saying so, with the folder
     date, and finish.
   - Missed week check: search Gmail for  subject:"ROAR 2.0 ·" newer_than:10d . If no digest was sent last
     week and a previous folder digests/<prev> exists, merge it in before analysing:
       python -m roar fetch --week-of <date> --no-state --from-items digests/<date>/candidates.json digests/<prev>/candidates.json
   - Run  python -m roar analyze --week-of <date>  once; it stops with "Waiting for analysis.json" and
     writes <folder>/analyst_prompt.md (your instructions and the reader profile) and
     <folder>/analyst_input.md (the candidates). Read both in full.

3. Do the analysis exactly as analyst_prompt.md describes: read every candidate, choose the ranked top 10,
   section items (including the breast section) and tidbits, write plain-English headlines and 2–3
   sentence summaries with the key numbers, never invent facts. Write the result as valid JSON to
   <folder>/analysis.json (week_of = the folder's date).

4. Validate and render:
     python -m roar analyze --week-of <date>      (must print "analysis ok"; if it reports dropped ids, fix
                                                   analysis.json and rerun)
     python -m roar render  --week-of <date>      (writes digest.html, digest.txt, subject.txt)

5. Email it with the Gmail send tool: to michaelgreen04@gmail.com, subject = contents of
   <folder>/subject.txt, htmlBody = the complete contents of <folder>/digest.html copied exactly (it is
   60–95 KB of inline-styled HTML; read it in full first and do not paraphrase, shorten or reformat it), body = contents of
   <folder>/digest.txt. Send exactly one email. If the send fails for a permissions/scope reason, save
   digest.html to /mnt/user-data/outputs/ and finish with a report that says the Gmail connector needs
   send permission.

6. Copy digest.html, digest.md and analysis.json to /mnt/user-data/outputs/ so they are visible in the
   session. Do not try to push to the repo (the session has no credentials for it).

7. Finish with a three-line report: number of candidates read, number selected, and the subject line.
```

## Why this split

The Cowork sandbox can reach github.com but not PubMed or publisher sites, so it cannot fetch. GitHub
Actions can fetch but has no LLM unless you pay for API access. Splitting the job keeps the whole thing
free: the Action commits `candidates.json`, the Cowork task clones the repo, reads the candidates, writes
`analysis.json`, renders and mails. The analyst instructions live in `roar/prompts/analyst.md` in the
repo, so you can tune them with a normal commit.

Because the Cowork task cannot write back to the repo, the repo only ever holds candidates (and
`state/seen.json`); the finished digests live in your inbox and in each task run's outputs. The Action
therefore runs `roar fetch --no-carry-over` — the "carry over an unrendered week" heuristic relies on
`digest.html` landing in the repo, which only happens in the all-local set-up (docs/WORK_COMPUTER.md).

## Timing

| step | when (UTC) | when (Alaska, summer) |
|---|---|---|
| GitHub Action fetch | Mon 06:15 | Sun 22:15 |
| Cowork task analyse + send | Mon 14:00 | Mon 06:00 |

Winter (AKST, UTC-9) shifts both an hour earlier in local time; adjust the cron if that matters.

## If a week is missed

The task's step 2 checks Gmail for last week's digest and, if none went out, folds the previous folder's
candidates into this week's analysis. Running the scheduled task manually ("Run now") also works at any
time.

## Alternative delivery without Gmail send permission

Store a Gmail app password as the GitHub secrets `ROAR_SMTP_USER` / `ROAR_SMTP_PASSWORD` / `ROAR_FROM`
plus `ROAR_TO`, add `ANTHROPIC_API_KEY`, and run the workflow with `backend=anthropic`, `send=true` (or
flip the defaults in `weekly.yml`). Then the whole job runs inside GitHub Actions and Cowork is not needed.
