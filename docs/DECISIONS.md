# Decisions

Version: 0.99.0
Date: 2026-11-15
Status: verified: each entry's decision, date and version against `CHANGELOG.md`, `goal.md` and the code named in it (read 14 Nov 2026). Where the specification does not record the alternatives considered, the entry says "not recorded" and gives only what the design implies. Unknown: nothing beyond what each entry states.

Short architecture decision records (context, decision, alternatives, consequence, version). "Owner" is the person who decided. Dates are 2026.

## 1. LLM execution: batch files, then headless Claude Code

- **Context.** The pipeline runs on a home NAS and must not use a pay-per-token API. Scoring and document writing still need a model.
- **Decision.** Option C (31 Oct): the NAS writes a batch file, a Claude Code session processes it and writes a results file, the NAS imports it. Option A (headless Claude Code on the NAS under the subscription login) was accepted on 3 Nov despite unclear terms and built in v0.78.0 (5 Nov), off by default; it uses the same files.
- **Alternatives.** A: headless on the NAS, fully scheduled. B: a scheduled task on the owner's computer. C: semi-manual (chosen first).
- **Consequence.** The file contract is the stable interface; the headless worker could be added without changing the importer. Cost: usage limits can cut a run short, so chunked results and carry-over exist (v0.69.0).
- **Version.** Option C recorded in 0.5.0; headless in 0.78.0.

## 2. SQLite

- **Context.** One user, one machine, daily batch writes, a few readers.
- **Decision.** One SQLite file in WAL mode on a mounted volume, with numbered migrations and the online backup API for backups (`goal.md` section 4, `app/db.py`, `app/backup.py`).
- **Alternatives.** A client-server database: not recorded in the specification.
- **Consequence.** One backup covers everything, including the archive table; the web app opens the file read-only for GET and never migrates it. Concurrency is limited, handled by a run lock.
- **Version.** Present from the first code release; backup API in 0.31.0.

## 3. Server-rendered web app

- **Context.** One user needs a Top 10 page, job detail, analytics and a few buttons.
- **Decision.** FastAPI with Jinja2 templates, plain forms and Post/Redirect/Get; Chart.js only on the analytics and health pages (`goal.md` section 4, 5.7; `app/web.py`).
- **Alternatives.** A single-page app: not recorded.
- **Consequence.** A strict Content-Security-Policy, CSRF tokens per action and job, no client state to keep in sync.
- **Version.** 0.42.0 (read-only), 0.46.0 (first writes).

## 4. Description resolver tiers, and dropping LinkedIn

- **Context.** A LinkedIn alert carries no description, so the job would get no CV.
- **Decision.** Look up the job on the employer's own site. Tier 1: written permission or a public job API. Tier 2: a public career page whose robots.txt allows it, at an accepted risk. Tier 3 (LinkedIn page, last resort) was planned in 0.63.0 and dropped in 0.65.0 (3 Nov) because LinkedIn's robots.txt disallows all crawling; robots.txt is respected in every tier.
- **Alternatives.** Reading pages through the owner's logged-in browser (an agent existed and was retired in 0.63.0); manual paste (kept as the fallback page).
- **Consequence.** Some jobs stay "description missing" and are reported with the tiers tried; they get no documents. Polite fetching is enforced in code: delays, daily caps per host, stop at the first 403, 429 or 5xx.
- **Version.** 0.63.0, 0.65.0.

## 5. Travel limits: 60 minutes on site, 120 hybrid

- **Context.** The first rule was 60 minutes by train or car (0.4.0, 31 Oct), replacing a distance rule.
- **Decision.** On 14 Nov: Swiss and Liechtenstein hybrid jobs pass up to 120 minutes; on-site stays at 60; an unknown work mode at 61 to 120 minutes is conditional; office-day counts never reject; the canton of Geneva always fails unless fully remote. Poland: remote or hybrid with low office presence passes.
- **Alternatives.** One limit for all work modes (the previous behaviour).
- **Consequence.** `recheck-archive-location` re-examines archived rejections with cached travel data and restores the ones that now pass.
- **Version.** 0.92.0.

## 6. German postings capped at 49

- **Context.** A posting written in German implies fluent German, which the profile does not have. Until then such jobs were capped at 69.
- **Decision.** The cap is 49, in the low band, below the document threshold of 60, so no CV or letter is written for them (prompt 1.17.0; `non_english_cap` in the config; the importer enforces it).
- **Alternatives.** Cap at 69 (the previous value).
- **Consequence.** A new command, `recheck-language --cap-scores`, lowers stored scores. The reason text shows the band.
- **Version.** 0.93.0.

## 7. Traceability check

- **Context.** A model drafting a CV can invent an employer, a number or a skill.
- **Decision.** One structured fact file is the source of truth. Every employer, title, date, number, skill and certificate in a generated document must exist in it; a failed check blocks the document. The check is pure Python and conservative: it prefers a false alarm to a missed fabrication. A language rule is not overridable (the profile's German level stays "basic" in every document). Blocked findings can be rechecked or accepted as false positives with a stored comment (0.83.0).
- **Alternatives.** Trusting the model; review by the model alone.
- **Consequence.** Documents are drafts until approved; the check runs at import and again at render.
- **Version.** 0.30.0 (the check), 0.56.0 (adjusted allowances).

## 8. Reviewer model gate (doc-reviewer)

- **Context.** Sonnet writes the CV drafts and letters; the owner wanted every document reviewed.
- **Decision.** From 2 Nov, `doc-reviewer` (Opus, low effort) reviews and corrects every CV and letter before the results file is imported. Letters moved from Opus to Sonnet in 0.58.0, with the review kept.
- **Alternatives.** The main session reviews (until 0.55.0); no review.
- **Consequence.** Wording quality rises at small Opus cost; the traceability check still decides facts.
- **Version.** 0.55.0, 0.58.0.

## 9. Model routing

- **Context.** Usage showed almost everything running on Opus.
- **Decision.** 2 Nov: a set of project agents and a delegation rule; the main session runs Sonnet at medium effort every day; Haiku for reading, running and exact edits; Opus only by escalation (0.58.0). On 3 Nov `opus-escalation` also became planner and designer for non-trivial work (0.66.1); its effort moved to high on 11 Nov (0.80.2).
- **Alternatives.** Opus main session with helpers (the earlier setup).
- **Consequence.** Cost depends on the number of Opus turns, which are few and short. The rules against overconfidence came with this change.
- **Version.** 0.55.0, 0.58.0, 0.66.1, 0.80.2.

## 10. Standing approval for pull requests and merges

- **Context.** An "ask first" rule (0.59.0) kept every action behind the owner's go.
- **Decision.** On 4 Nov the owner approved pull requests and merges without a per-PR go, after the checks pass. Announcing before and reporting after still apply, and asking first still holds for agents, NAS edits and database-writing runs.
- **Alternatives.** Per-PR approval.
- **Consequence.** Shorter loop; the risk is carried by the checks and by keeping hard-to-revert changes in their own release.
- **Version.** 0.77.2.

## 11. No GitHub Actions

- **Context.** The free Actions minutes were used up on 12 Nov.
- **Decision.** The local test suite is the gate before every merge and its result goes in the pull request; tag and release are made with `gh release create`; the Docker image is built and pushed from the owner's PC.
- **Alternatives.** Paying for minutes; stopping automated checks.
- **Consequence.** The CI guard against committed secrets and database files still exists in the workflow file, but is not the gate.
- **Version.** 0.84.0.

## 12. SemVer for code and documents

- **Context.** The specification and the code change together, and the owner wanted to see what changed when.
- **Decision.** Semantic Versioning for everything, documents included (0.3.0, `goal.md` 7.5): a change that releases carries the bump, a changelog entry and matching `Version:` headers; tags are lightweight and the release carries the annotation.
- **Alternatives.** Not recorded.
- **Consequence.** 185 releases in 16 days; the cost is bookkeeping, which is why changes are batched into one release window (0.62.0).
- **Version.** 0.3.0; 0.4.1 for the tagging rule.

## 13. Public copy

- **Context.** The private repository holds the owner's real profile, configuration and operating notes. The project is also meant as a portfolio piece.
- **Decision.** A sanitised public copy named `job-match-pipeline`, with a fictional persona and without the private history. The README, the architecture page, this page and the AI workflow page are written for that copy; operating documents sit in a marked private block.
- **Alternatives.** Making the private repository public: not recorded as considered; it holds personal data.
- **Consequence.** Numbers in the README are measured in the private repository and labelled so.
- **Version.** 0.94.0.

## 14. Bulk status: one token per action, the same rules as the single buttons

- **Context.** On the Applications pages and on the phone the same status change is often made for several jobs at once.
- **Decision.** A bulk change is a POST to `/bulk-status` with a token per action (`csrf("bulk:<action>")`), the value `confirm=yes` and at most 200 jobs; each job is checked and changed in its own transaction under the write lock, so a busy database stops at that job and the message says what was done. Without JavaScript a GET confirmation page does the same. The single routes are unchanged.
- **Alternatives.** Not recorded. 0.97.0 added no JSON or fetch endpoint (goal.md 5.7, "Out").
- **Consequence.** The sheet on the list pages carries the per-action token in a data attribute; it is the same token the confirmation page already shows, so it is not a new secret.
- **Version.** 0.96.0 (route, CLI and pages); 0.97.0 (the sheet and select.js); `goal.md` 5.7 N10, Q4, Q8.

## 15. Scripts only on the eight list pages

- **Context.** Until 0.96.0 no page of the web app ran a script (`script-src 'none'` everywhere).
- **Decision.** Only the eight list pages (`/`, `/jobs`, `/missing`, `/applied`, `/interviews`, `/offers`, `/rejected`, `/withdrawn`, the tuple `web.SCRIPT_PATHS`) send `script-src 'self'` and one script tag, `/static/select.js`. select.js has no inline code, no eval and no network call, and it hard-codes no status, action or label: everything it needs comes from the server-rendered attributes. The job pages, the confirmation pages, `GET /bulk-status`, the login and the static files keep `'none'`.
- **Alternatives.** Not recorded.
- **Consequence.** A bug in select.js is the new risk. It is checked by a static test (no unsafe API, size, `"use strict"`), by `node --check` where node is installed, and by review of each diff; without JavaScript the confirmation page still works.
- **Version.** 0.97.0; `goal.md` 5.7 N13.

## 16. No "accepted" status yet

- **Superseded by 18 (0.98.0).** The text below is kept as the history of 0.96.0 and 0.97.0.
- **Context.** An accepted offer needs a status of its own and so a database migration, which the project keeps in its own release.
- **Decision.** Versions 0.96.0 and 0.97.0 add no status: `withdrawn` after an offer is labelled "Declined" and `employer_rejected` is labelled "Offer withdrawn by employer" on the Offers page; `db.STATUSES` is unchanged. "Accepted", interview dates and notes come in 0.98.0 with the next free migration number, in a PR of their own.
- **Alternatives.** Not recorded.
- **Consequence.** The Offers page uses the two labels above; the names in the database do not change.
- **Version.** 0.96.0 and 0.97.0; `goal.md` 5.7 N1.

## 17. An employer's rejection is not an archive

- **Context.** A rejection by the employer is the result of an application, not a filter outcome.
- **Decision.** "Rejected by employer" (status `rejected`, reached from applied, interview or offer) stays on the Applications pages and is not archived. Only Reject (archive, reason `user_rejected`) archives a job; it is offered on Applied only, not on Interviews, Offers, Rejected by employer or Withdrawn.
- **Alternatives.** Not recorded.
- **Consequence.** The statistics count the job as an application that was rejected; a job the user wants out of the lists is archived with Reject before it is applied for.
- **Version.** 0.96.0; `goal.md` 5.3.3 and 5.7 N3.

## 18. Accepted, interview rounds and notes

- **Context.** An offer that is accepted needs a status of its own, and interview rounds and notes need storage. Both need a database migration (entry 16 deferred them to 0.98.0).
- **Decision.** Migration 25 (schema 24 -> 25) adds the status `accepted`, reached only from `offer` and undone only with Undo, and the tables `interviews` and `job_notes`. Times are stored as UTC and entered and shown in the config `timezone` (Europe/Zurich). Archive keeps the rounds and notes; restore puts them back, but not the old status-history rows, which stay in the archive record. Editing a round, the "Next interview" column and interview statistics are deferred to 0.99.0.
- **Alternatives.** A separate status for a declined offer was rejected (it stays `withdrawn`, goal.md 5.7 OD3). Editing notes was not chosen (delete only).
- **Consequence.** Schema 25: the 0.97.0 code refuses the database, so a rollback means restoring the backup taken before the deploy (DEPLOYMENT_MANUAL, migration section). Notes and rounds are removed with an archived job by the foreign-key cascade in 0.97.0. Decided by the user on 15 Nov 2026 (v0.98.2): deleting a round or a note asks first with a confirmation dialog; required, built in 0.99.0.
- **Version.** 0.98.0; `goal.md` 5.7 "Decisions for 0.98.0"; supersedes entry 16.

## 19. Delete confirmation popup, and the interview features of 0.99.0

- **Context.** Deleting an interview round or a note removed it at once. The owner asked for a confirmation first (v0.98.2, 15 Nov 2026).
- **Decision.** Decided by the user on 15 Nov 2026: a native JavaScript `confirm()` popup, chosen over a server confirmation page. The job page's Content-Security-Policy is `script-src 'self'` (that page only; this changes the `'none'` rule of entry 15 for the job page); the other pages keep their policy. The server still requires `confirm=yes`; without JavaScript a delete shows a confirmation page. Also in 0.99.0: rounds can be edited; the "Next interview" column is on `/interviews` only; Undo out of interview keeps the round; the Interview sheet has an optional first-round date for a single job, written in the same transaction, and its default kind is "phone"; `/analytics` shows rounds and kinds, upcoming rounds, rounds per job, and the days from application to the first round and between rounds. No migration; schema stays 25.
- **Alternatives.** A server confirmation page (kept as the fallback without JavaScript).
- **Consequence.** The popup logic is not run by the tests (no JavaScript runtime there; `node --check` only). The confirm popup on iPhone Safari is a manual check, pending.
- **Version.** 0.99.0; `goal.md` 5.7 "Decisions for 0.99.0".
