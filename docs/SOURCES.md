# Sources

ROAR 2.0 reads two kinds of source every week. Both are plain YAML you can edit.

## PubMed queries — `config/pubmed.yaml`

PubMed is the backbone for journal literature: it covers every journal, returns abstracts and publication
types, and is free to query (E-utilities). Six queries run over the past 8 days (`datetype=edat`, the date
the record entered PubMed, so nothing waits for MeSH indexing):

| id | what it catches |
|---|---|
| `rt_all` | any paper whose title/abstract mentions radiotherapy-type terms **and** a clinical design word (randomised, phase 2/3, guideline, meta-analysis, PROs, toxicity, cost…) |
| `radonc_journals` | everything in the radiation oncology journals (Red Journal, Green Journal, PRO, Advances, ctRO, Seminars, Clinical Oncology, Brachytherapy, Radiation Oncology, Strahlentherapie, phiRO, Med Phys, JACMP, PMB…) |
| `top_journals_onc` | oncology papers in NEJM, Lancet, JAMA, Lancet Oncol, JCO, JAMA Oncol, Annals, Nature Medicine, JNCI, Cancer, CA, NRCO, EJC, JAMA Netw Open, BMJ, Blood, Lancet Haem, JTO, Eur Urol, Gyn Onc, IJGC, Head & Neck, Oral Oncol, JAMA Derm, JAAD, Neuro-Onc, CCR, ESMO Open, JCO OP, GI/hepatology/surgery journals… with a trial/guideline/RT/screening word |
| `ai_medicine` | AI / LLM papers restricted to high-impact general, oncology, radiology and radonc journals |
| `screening` | cancer screening / early detection restricted to high-impact journals |
| `breast` | breast-cancer trials, guidelines and radiotherapy papers in the top general, radiation-oncology and breast journals (added 2026-09-15 when breast became a regular section) |

Every query excludes obvious bench science. Fragments (`{rt_terms}` etc.) are
shared between queries so a change in one place propagates.

## RSS / Atom feeds — `config/sources.yaml`

Feeds add what PubMed cannot: news, society and regulatory items, preprints, and journal "in press" lists
that appear days before PubMed indexes them. All URLs were checked against Feedly's index on 2026-09-15
(velocity = items/week).

**Cloud-IP blocking.** The first run from GitHub Actions (2026-09-15) showed that Elsevier journal sites
(Red Journal, PRO, Green Journal, Advances, Clinical Oncology, Brachytherapy, EJC, Eur Urol, JTO, Gyn Onc,
The Breast, Clinical Breast Cancer, Annals of Oncology), Wiley (Medical Physics, JACMP, Cancer, CA, Head &
Neck), The Lancet family, NEJM, OncLive and Substack answer HTTP 403 to requests from cloud IP ranges. The
fetcher therefore tries three routes per feed: the polite ROAR user agent, then browser-like headers, then
Feedly's public stream cache (`cloud.feedly.com/v3/streams/contents?streamId=feed/<url>`), which keeps
polling those feeds from its own servers. `python -m roar verify-sources` prints which route worked
(`via browser-ua` / `via feedly`). Every journal in the blocked list is also covered by the PubMed queries,
so a feed that fails outright costs at most a few days of lead time on "in press" items.

| feed | tier | velocity | note |
|---|---|---|---|
| Red Journal in press / current | 1 | ~11 | |
| Practical Radiation Oncology in press / current | 1 | ~5 | |
| Radiotherapy & Oncology in press | 1 | ~6 | |
| Advances in Radiation Oncology | 2 | ~9 | |
| Clinical Oncology (RCR) | 2 | ~15 | |
| Brachytherapy | 2 | ~2 | |
| Seminars in Radiation Oncology (ScienceDirect) | 2 | ~3 | |
| Clinical & Translational Radiation Oncology | 2 | ~6 | |
| Physics & Imaging in Radiation Oncology | 3 | ~4 | |
| Medical Physics | 3 | ~8 | |
| JACMP | 3 | ~11 | disabled by default (mostly QA) |
| JCO / JCO Oncology Practice | 1 / 2 | ~8 / ~5 | |
| Lancet Oncology online first | 1 | ~3 | |
| NEJM | 1 | ~19 | topic-filtered |
| JAMA Oncology online first / JAMA online first | 1 | ~6 / ~34 | JAMA topic-filtered |
| Annals of Oncology | 1 | ~3 | |
| JNCI advance articles | 2 | ~12 | |
| Cancer / CA: A Cancer Journal for Clinicians | 2 / 1 | ~8 / ~2 | |
| Nature Reviews Clinical Oncology | 1 | ~1 | |
| Nature Medicine / Nature Cancer | 1 / 2 | ~16 / ~3 | topic-filtered |
| British Journal of Cancer | 3 | ~7 | |
| European Journal of Cancer in press | 2 | ~8 | |
| ESMO Open | 3 | ~7 | |
| The Lancet online first | 1 | ~15 | topic-filtered |
| European Urology in press | 2 | ~4 | site: GU |
| Head & Neck / Oral Oncology | 3 | ~7 / ~5 | site: HN |
| Journal of Thoracic Oncology | 2 | ~5 | site: thoracic |
| Lancet Haematology | 2 | ~1 | site: heme |
| Neuro-Oncology advance | 3 | ~6 | site: CNS |
| Gynecologic Oncology | 2 | low | site: GYN (PubMed covers the rest) |
| The Breast / npj Breast Cancer | 2 / 2 | ~3 / ~4 | site: breast |
| Clinical Breast Cancer / Breast Cancer Res Treat | 3 / 3 | ~6 / pattern-based | site: breast |
| Journal of Breast Imaging advance access | 3 | ~1 | site: breast, topic-filtered (screening/imaging) |
| Lancet Digital Health / npj Digital Medicine | 2 / 3 | ~5 / ~20 | topic-filtered |
| medRxiv oncology preprints | 3 | ~12 | topic-filtered |
| The ASCO Post | 2 | ~20 | news |
| OncLive | 3 | ~7 | news |
| Healio Hematology/Oncology | 3 | ~13 | news |
| Targeted Oncology / CancerNetwork | 3 | ~50 each | disabled by default (volume) |
| NCI Cancer Currents / NCI news releases | 2 | low | |
| FDA press announcements | 2 | ~3 | topic-filtered (oncology approvals) |
| The Cancer Letter | 3 | ~7 | paywalled; headlines only |
| Radiation Medicine: Dollars and Sense (Substack) | 3 | low | rad-onc economics/policy |
| The Accelerators Podcast | 3 | stale | disabled |

### No RSS available (checked)

ASTRO news and ASTROblog, ESTRO news, NCCN guideline updates, USPSTF recommendations, the FDA
"Oncology/Hematologic Malignancies Approval Notifications" page, ASCO Daily News, MedPage Today. These are
email-only. Coverage comes indirectly: FDA press releases (approvals), PubMed (guideline papers appear in
journals), ASCO Post/OncLive/Healio (society and meeting news).

## Compared with the old Feedly set-up

Feedly followed 29 journal feeds with no topic filter, so JAMA Network Open (~225 stories/month) and BMJ
Open (~150/month) alone made up most of the stream, almost none of it oncology. ROAR keeps the good
journals, drops the two firehoses (their oncology papers are still caught through the PubMed journal
filter), adds the radiation-oncology-specific journals that were missing (Green Journal in press, Advances,
Clinical Oncology, Brachytherapy, ctRO, phiRO), and adds news/regulatory/AI sources Feedly did not have.
