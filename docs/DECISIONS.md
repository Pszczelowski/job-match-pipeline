<!--
Version: 0.105.0
Date: 2026-11-17
Status: verified: each decision, date and version against CHANGELOG.md and goal.md (read 14 to 17 Nov 2026).
-->

# Key decisions

The choices that shaped the project, in the order they were made. Each one: what I decided, and what it gave me.

| Date | Decision | What it gave |
|---|---|---|
| 31 Oct | **Every change gets a version number**, documents included | A clear record of what changed and when: 190+ releases in 16 days |
| 31 Oct | **AI work through plain job files** instead of a paid API | No API costs, and the app and the AI can be tested apart |
| 31 Oct | **One small database file** (SQLite) | One backup covers everything; nothing to administer |
| 31 Oct | **Hard rules before AI**: language, commute, work mode | Fast, explainable filtering; the AI only judges what is left |
| 1 Nov | **A fact checker for every document** | No invented employers, numbers or skills; ever |
| 1 Nov | **Never overstate a language level** | Honest documents, enforced in code |
| 1 Nov | **A simple, server-rendered web app** | Fast pages, strong security, little to maintain |
| 2 Nov | **A team of AI agents with a model per role** | Lower cost, better focus, a stronger model where quality matters |
| 2 Nov | **A reviewer model checks every CV and letter** | Better wording on top of the fact check |
| 3 Nov | **Ask before acting**: agents start nothing without my go | I stay in control of what changes |
| 4 Nov | **Standing approval for merges once the tests pass** | A faster loop, with the tests as the safety net |
| 5 Nov | **The AI runs on its own on a home server** | A daily run with no manual steps |
| 12 Nov | **Local tests as the merge gate** after the free build minutes ran out | Testing never stopped |
| 14 Nov | **Hybrid jobs may be up to 120 minutes away**, on-site 60 | Fewer good jobs filtered out; old rejections rechecked |
| 14 Nov | **German-language postings score low** | No documents for jobs that need fluent German |
| 14 Nov | **A sanitised public copy** with a fictional persona | A portfolio piece with no personal data |
| 15 Nov | **Application stages through to Accepted**, with interview rounds and notes | The whole application journey in one place |
| 15 Nov | **Bulk changes and a confirmation before deleting** | Quicker on a phone, and no accidental loss |
| 16 Nov | **ATS-friendly CVs and a landing page** | Documents that read well for people and for screening software |

## Three that mattered most

**The fact checker (1 Nov).** A model drafting a CV can invent things. Instead of trusting it, every fact in a document must exist in my real CV file, or the document is blocked. It prefers a false alarm to a missed mistake, and blocked findings can be reviewed in the web app.

**Plain job files instead of an API (31 Oct).** The app writes a file of jobs; the AI writes back a file of results; both sides check the format. This kept costs at zero, let the AI worker move from manual runs to a fully automatic one (5 Nov) without touching the rest, and made the whole chain easy to test.

**An AI team with clear roles (2 Nov).** Splitting the work by model size, with written house rules, made the agents cheaper and more reliable at the same time. See [Working with AI agents](AI_WORKFLOW.md).

[← Back to the documentation](README.md)
