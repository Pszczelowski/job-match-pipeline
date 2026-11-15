# Decisions

Version: 0.94.0
Date: 2026-11-14
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
