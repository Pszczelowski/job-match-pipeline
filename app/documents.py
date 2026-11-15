"""Tailored documents from a results file of schema 1.1 (goal.md 5.6, 5.10).

The session writes, for a job in the top places above the cut-off, the text of a cover letter and a CV: selection,
order and wording only. This module decides which jobs qualify, checks what the session wrote and stores it:

* which jobs: the batch's NAS-selected fresh jobs with rank <= `tailoring.documents_top_n` and an `overall` of at
  least `tailoring.min_score` (`eligible_jobs`). The NAS decides, not the file.
* the checks: the traceability check of `app/traceability.py` (employers, titles, dates, numbers, skills,
  certificates, cited fact ids), plus what only this module knows: the CV variant, experience ids and fact ids
  against `profile/master_cv.yaml` (a fact must belong to the experience it is listed under), skill-group ids, the
  contact person (a name must stand in the job description; the session must not invent one) and the home address
  (it must not appear). A document with a finding is stored as `blocked`, the rest as `draft`; nothing is dropped.
* the storage: table `documents`, one row per job and kind, replaced by a later file for the same job.

Employers, titles and dates of a CV, the letter header, the date and the salutation are made by code (slices 3c and
3d, `app/pdf.py`); they are never taken from the session. No network, no secrets.

* the PDFs (slices 3c and 3d): after the import, every `draft` document is rendered into `paths.documents`, one folder
  per job (`render_stored`). The path (relative to that folder), the page count and the time go into the row. A PDF
  that is longer than its limit (letter 1 page, CV 2 pages; `tailoring.letter_max_pages`, `tailoring.cv_max_pages`) is
  kept and the document stays a draft; the warning `pdf_over_page_limit` is added to its findings and shown in the
  report. Blocked documents get no PDF.

The approval step (slice 3e, `app/approval.py`): the user approves a `draft` that has its PDF. An `approved` document is
frozen: a later results file does not replace it (`DocumentOutcome.frozen`), `render_stored` leaves it alone, and its PDF
is kept as a copy in `paths.approved`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from . import db, pdf, traceability as tr
from .batch import PROFILE_FILES
from .config import ROOT, resolve_path
from .filters import language as language_filter

log = logging.getLogger("recruitment")

KINDS = ("cover_letter", "cv")
REVIEW_ATTENTION = "review_attention"        # a finding the headless reviewer raised (v0.78.0); the document stays a draft
PDF_WARNING_KINDS = (pdf.PAGE_WARNING, "render_error", pdf.LINE_ENDING_WARNING)      # findings made by the PDF step; they never block a document
LENGTH_WARNING = "length"                    # a letter outside 290-350 words (goal.md 5.2); a warning like the PDF ones, it never blocks
LETTER_WORDS_MIN, LETTER_WORDS_MAX = 290, 350
WARNING_KINDS = PDF_WARNING_KINDS + (LENGTH_WARNING,)


# Findings the user may accept as false positives on the job page (v0.83.0). The language and address findings may not be.
NOT_OVERRIDABLE = ("language_claim", "german_not_stated", "address")
SHOWN_AS_WARNING = WARNING_KINDS + (REVIEW_ATTENTION,)    # never block (the reviewer's flag does not change the status)


def finding_key(f: dict) -> tuple[str, str, str]:
    return (f["kind"], f.get("where") or "", f.get("text") or "")


def overridable(f: dict) -> bool:
    return f["kind"] not in NOT_OVERRIDABLE and f["kind"] not in SHOWN_AS_WARNING


def load_overrides(conn, document_id: int) -> set[tuple[str, str, str]]:
    return {(r["kind"], r["finding_where"], r["text"]) for r in conn.execute(
        "SELECT kind, finding_where, text FROM finding_overrides WHERE document_id = ?", (document_id,))}


def has_blocking(findings: list[dict], overrides=()) -> bool:
    """A finding blocks unless it is a warning or the user accepted it as a false positive (`overrides`: finding keys)."""
    return any(f["kind"] not in WARNING_KINDS and not (overridable(f) and finding_key(f) in overrides) for f in findings)


@dataclass
class FactIndex:
    """What the structural checks need from `profile/master_cv.yaml` and `profile/variants.yaml`."""
    experience_bullets: dict[str, set[str]] = field(default_factory=dict)     # experience id -> ids of its bullets
    fact_owner: dict[str, str] = field(default_factory=dict)                  # bullet id -> experience id
    skill_groups: set[str] = field(default_factory=set)
    variants: set[str] = field(default_factory=set)


def load_fact_index(master_path=None, variants_path=None) -> FactIndex:
    master = yaml.safe_load((master_path or ROOT / PROFILE_FILES["master_cv"]).read_text(encoding="utf-8")) or {}
    variants = yaml.safe_load((variants_path or ROOT / PROFILE_FILES["variants"]).read_text(encoding="utf-8")) or {}
    index = FactIndex(variants=set(variants.get("variants", {})))
    for exp in master.get("experience", []):
        ids = {b["id"] for b in exp.get("bullets", []) if isinstance(b.get("id"), str)}
        index.experience_bullets[exp["id"]] = ids
        index.fact_owner.update({i: exp["id"] for i in ids})
    index.skill_groups = {g["id"] for g in master.get("skills", []) if isinstance(g.get("id"), str)}
    return index


NO_DESCRIPTION_REASON = "no description (scored from the title only); add it with `missing` / `set-description`"


def eligible_jobs(conn, batch_id: str, cfg: dict) -> dict[int, int]:
    """job id -> overall for the jobs of a batch that get documents: selected, fresh, rank within
    `tailoring.documents_top_n`, latest overall at least `tailoring.min_score`, and (with
    `tailoring.require_description`) a stored description: a job scored from its title only gets none."""
    tl = cfg["tailoring"]
    rows = conn.execute(
        """SELECT bj.job_id, s.overall, j.description_missing FROM batch_jobs bj
           JOIN scores s ON s.id = (SELECT MAX(id) FROM scores WHERE job_id = bj.job_id)
           JOIN jobs j ON j.id = bj.job_id
           WHERE bj.batch_id = ? AND bj.selected = 1 AND bj.fresh = 1 AND bj.rank <= ? ORDER BY bj.rank""",
        (batch_id, tl["documents_top_n"])).fetchall()
    return {r["job_id"]: r["overall"] for r in rows
            if r["overall"] >= tl["min_score"] and not (tl["require_description"] and r["description_missing"])}


def not_eligible_reason(conn, batch_id: str, job_id: int, cfg: dict) -> str:
    tl = cfg["tailoring"]
    row = conn.execute(
        """SELECT bj.fresh, bj.selected, bj.rank, s.overall, j.description_missing FROM batch_jobs bj
           LEFT JOIN scores s ON s.id = (SELECT MAX(id) FROM scores WHERE job_id = bj.job_id)
           LEFT JOIN jobs j ON j.id = bj.job_id
           WHERE bj.batch_id = ? AND bj.job_id = ?""", (batch_id, job_id)).fetchone()
    if row is None or row["overall"] is None:
        return "no score (archived or not scored)"
    if not row["fresh"]:
        return "carried over from an earlier batch"
    if tl["require_description"] and row["description_missing"]:           # before the selection check: a queued job is not selected (v0.72.0)
        return NO_DESCRIPTION_REASON
    if not row["selected"] or row["rank"] > tl["documents_top_n"]:
        return f"not among the first {tl['documents_top_n']} selected jobs"
    return f"score {row['overall']} is below the cut-off of {tl['min_score']}"


# ----------------------------------------------------------------------------- checking

def _finding(kind: str, text: str, where: str, reason: str, block_text: str = "") -> dict:
    return {"kind": kind, "text": text, "block": None, "where": where, "reason": reason, "block_text": block_text[:140]}


_FORMAT_CHARS = "".join(c for c in map(chr, range(0x110000)) if unicodedata.category(c) == "Cf")      # soft hyphen, zero-width, bidi marks


def _strip_format(text: str) -> str:
    """Without the invisible formatting characters (Unicode category Cf): an address split by a soft hyphen is still the address."""
    return text.translate({ord(c): None for c in _FORMAT_CHARS}) if any(unicodedata.category(c) == "Cf" for c in text) else text


def _norm(text: str) -> str:
    return " " + " ".join(tr.words(_strip_format(text))) + " "


def _contact_named(contact: str, description: str) -> bool:
    """The contact is named in the description: whole words after _norm; or, as a fallback, the name followed directly by an
    uppercase letter, because the scraper glued the next word on ("Mélanie MoosHuman Resources"). A name glued to the word
    before it, or only the start of a longer word ("Moosbrugger"), does not count."""
    if _norm(contact) in _norm(description):
        return True
    want = tr.words(contact)
    if not want:
        return False
    runs = re.findall(r"[^\W_]+", description)
    joined = ["".join(tr.words(r)) for r in runs]
    n = len(want)
    for i in range(len(runs) - n + 1):
        if tuple(joined[i:i + n - 1]) != want[:-1]:
            continue
        last = runs[i + n - 1]
        for j in range(1, len(last)):
            if last[j - 1].islower() and last[j].isupper() and "".join(tr.words(last[:j])) == want[-1]:
                return True
    return False


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def _from_trace(result: tr.TraceResult, labels: list[str]) -> list[dict]:
    out = []
    for f in result.findings:
        if f.block < 0:
            continue        # the title line is the job's own title ("80-100%", "IAM Business Analyst"), not a claim about the user: no kind of finding on it blocks a document
        d = f.to_dict()
        d["where"] = "title line" if f.block < 0 else labels[f.block] if f.block < len(labels) else f"block {f.block + 1}"
        out.append(d)
    return out


def check_cover_letter(content: dict, job: dict, facts: tr.Facts, generated_on: date, home_address: str = "",
                       german_required: bool = False) -> list[dict]:
    """Findings for a cover letter (`content` is the validated `cover_letter` object of the results file).
    `german_required`: the job asks for German; the letter must then say that the user's German is basic."""
    b = content["blocks"]
    blocks, labels = [tr.Block(b["opening"])], ["opening"]
    blocks.append(tr.Block(b["lead_in"]))
    labels.append("lead_in")
    for i, a in enumerate(b["achievements"], 1):
        blocks.append(tr.Block(f"{a['label']}: {a['text']}", tuple(a["facts"])))
        labels.append(f"achievement {i}")
    blocks += [tr.Block(b["skills"]), tr.Block(b["closing"])]
    labels += ["skills", "closing"]
    contact = content.get("contact_name")
    doc = tr.Document(blocks, title=job["title"], company=job["company"], posting=f"{job['title']}\n{job['description'] or ''}")
    result = tr.check_document(doc, facts, allowed_employers=[job["company"]], generated_on=generated_on,
                               employer_location=", ".join(job["locations"]) or None, contact=contact)
    findings = _from_trace(result, labels)
    if contact and not _contact_named(contact, job["description"] or ""):
        findings.append(_finding("contact", contact, "contact_name",
                                 "this name is not in the job description; the contact must be named in the posting, never invented"))
    findings += _language_claim_findings([(labels[i], blocks[i].text) for i in range(len(blocks))])
    if german_required and not any(_states_basic_german(blk.text) for blk in blocks):
        findings.append(_finding("german_not_stated", "(German level)", "document", GERMAN_NOT_STATED_REASON))
    n_words = sum(len(blk.text.split()) for blk in blocks)
    if not LETTER_WORDS_MIN <= n_words <= LETTER_WORDS_MAX:
        findings.append(_finding(LENGTH_WARNING, f"{n_words} words", "document",
                                 f"the letter has {n_words} words; the target is {LETTER_WORDS_MIN} to 330, at most {LETTER_WORDS_MAX}"))
    return findings + _address_findings(content, home_address)


def check_cv(content: dict, job: dict, facts: tr.Facts, index: FactIndex, generated_on: date, home_address: str = "") -> list[dict]:
    """Findings for a CV (`content` is the validated `cv` object of the results file)."""
    findings: list[dict] = []
    blocks, labels = [tr.Block(content["summary"])], ["summary"]
    if content["variant"] not in index.variants:
        findings.append(_finding("variant", content["variant"], "variant",
                                 f"not a CV variant of profile/variants.yaml; known: {sorted(index.variants)}"))
    seen_experiences: set[str] = set()
    for i, exp in enumerate(content["experience"], 1):
        exp_id = exp["experience_id"]
        known = exp_id in index.experience_bullets
        if not known:
            findings.append(_finding("unknown_fact_id", exp_id, f"experience {i}", "this experience id does not exist in the fact file"))
        elif exp_id in seen_experiences:
            findings.append(_finding("duplicate", exp_id, f"experience {i}", "this experience is listed twice"))
        seen_experiences.add(exp_id)
        if known and not exp["bullets"]:
            findings.append(_finding("empty_experience", exp_id, f"experience {i}",
                                     "this experience has no bullet; every experience of the fact file needs at least one"))
        for j, bullet in enumerate(exp["bullets"], 1):
            where = f"experience {i} ({exp_id}), bullet {j}"
            fid = bullet["fact_id"]
            blocks.append(tr.Block(bullet["text"], (fid,)))
            labels.append(where)
            owner = index.fact_owner.get(fid)
            if known and owner is None:
                findings.append(_finding("fact_experience", fid, where,
                                         f"this id is not a bullet of the fact file; a CV bullet cites one of the bullets of {exp_id}",
                                         bullet["text"]))
            elif known and owner != exp_id:
                findings.append(_finding("fact_experience", fid, where,
                                         f"this fact belongs to {owner}, not to {exp_id}", bullet["text"]))
    for missing in sorted(set(index.experience_bullets) - seen_experiences):
        findings.append(_finding("missing_experience", missing, "experience",
                                 "this experience of the fact file is missing; the CV must list every experience, "
                                 "with at least one bullet (the oldest, least relevant ones with exactly one)"))
    for group in content.get("skills", []):
        if group not in index.skill_groups:
            findings.append(_finding("skill_group", group, "skills", "this skill-group id does not exist in the fact file"))
    doc = tr.Document(blocks, title=job["title"], company=job["company"])
    result = tr.check_document(doc, facts, allowed_employers=[job["company"]], generated_on=generated_on)
    findings += _language_claim_findings([(labels[i], blocks[i].text) for i in range(len(blocks))])
    return _from_trace(result, labels) + findings + _address_findings(content, home_address)


def _address_findings(content: dict, home_address: str) -> list[dict]:
    """The home address (config `location.car_origin_address`) must not appear in a document."""
    needle = _norm(home_address).strip()
    if len(needle) < 5:
        return []
    if any(needle in _norm(s) for s in _strings(content)):
        return [_finding("address", "(home address)", "document", "the text contains the home address, which must never be in a document")]
    return []


def check_content(kind: str, content: dict, job: dict, facts: tr.Facts, index: FactIndex, generated_on: date, home_address: str,
                  german_required: bool) -> list[dict]:
    """The checks of one document of either kind: the one place import, recheck and a revision call (v0.85.0)."""
    if kind == "cover_letter":
        return check_cover_letter(content, job, facts, generated_on, home_address, german_required)
    return check_cv(content, job, facts, index, generated_on, home_address)


# --- "Ask the writer to fix" (v0.85.0, goal.md 5.10a): the home address never goes to the llm container ------------

ADDRESS_MARKER = "[address removed]"
_ADDRESS_TOKEN_RE = re.compile(r"(?:[^\W_]|[+#]|[" + re.escape(_FORMAT_CHARS) + "])+")


def content_sha(content_text: str) -> str:
    """sha256 (hex) of the stored `documents.content` text: the request names the text it was made for."""
    return hashlib.sha256(content_text.encode("utf-8")).hexdigest()


def revisable(row) -> bool:
    """A document the user may ask the writer to fix: a blocked one (an address finding included, the address is scrubbed)."""
    return row is not None and row["status"] == "blocked"


def _address_needles(home: str) -> list[tuple[str, ...]]:
    """Folded word runs of the address and of its street part (the text before the first comma), longest first. Empty for an
    address under 5 folded characters (the threshold of `_address_findings`); the town alone is never a needle."""
    out: list[tuple[str, ...]] = []
    for part in (home or "", (home or "").split(",")[0]):
        w = tr.words(part)
        if len(" ".join(w)) >= 5 and w not in out:
            out.append(w)
    return sorted(out, key=len, reverse=True)


def _scrub_string(text: str, needles: list[tuple[str, ...]]) -> tuple[str, int]:
    count = 0
    for _ in range(5):                                       # until nothing matches (bounded)
        words: list[tuple[str, int, int]] = []               # folded word, start and end of its ORIGINAL token: fold changes lengths
        for m in _ADDRESS_TOKEN_RE.finditer(text):
            words += [(w, m.start(), m.end()) for w in tr.words(_strip_format(m.group(0)))]
        spans: list[tuple[int, int]] = []
        for needle in needles:
            n = len(needle)
            for i in range(len(words) - n + 1):
                if tuple(w[0] for w in words[i:i + n]) == needle:
                    spans.append((words[i][1], words[i + n - 1][2]))
        spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
        kept: list[tuple[int, int]] = []
        for s in spans:
            if not kept or s[0] >= kept[-1][1]:
                kept.append(s)
        if not kept:
            break
        for a, b in reversed(kept):
            text = text[:a] + ADDRESS_MARKER + text[b:]
        count += len(kept)
    return text, count


def scrub_address(obj, home: str):
    """(copy of `obj` with every string cleaned, number of spans replaced by ADDRESS_MARKER). A span is the address (or its street
    part) as `_address_findings` matches it: case, accents and punctuation between the words do not matter. Keys stay as they are."""
    needles = _address_needles(home)
    total = 0

    def walk(node):
        nonlocal total
        if isinstance(node, str):
            text, n = _scrub_string(node, needles) if needles else (node, 0)
            total += n
            return text
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node
    return walk(obj), total


def address_trace(text: str, home: str) -> bool:
    """True when `text` still holds the address or its street part: as words (`_address_findings` rule), as raw text or in folded
    form (case-insensitive). The guard before a request file is written: refuse rather than leak."""
    text = _strip_format(text)
    folded_text, lower_text, norm_text = tr.fold(text), text.lower(), _norm(text)
    for part in (home or "", (home or "").split(",")[0]):
        w = tr.words(part)
        if len(" ".join(w)) < 5:
            continue
        if _norm(part).strip() and _norm(part) in norm_text:
            return True
        raw = part.strip()
        if raw and (raw.lower() in lower_text or tr.fold(raw) in folded_text):
            return True
    return False


# --- German level (decided by the user on 1 Nov 2026, goal.md 5.6) -----------------------------------------------
# The facts say the user's German is basic. No document may claim more (`language_claim`), and a cover letter for a job
# that requires German must say plainly that it is basic (`german_not_stated`). Plain pattern matching, kept simple and
# conservative; the limits are listed in goal.md 5.6 and docs/DEVELOPMENT.md.

GERMAN_NOT_STATED_REASON = ("the job requires German; the letter must say plainly that your German is basic "
                            "(for example: My English and Polish are fluent; my German is basic.)")
_LANGUAGE_NAMES = (r"german|deutsch\w*|english|englisch\w*|polish|polnisch\w*|french|franz\w+|italian\w*|spanish|czech|"
                   r"russian|ukrainian|dutch|portuguese|slovak")
# "German market", "German banks": the word is an adjective of something else, not a language skill
_GERMAN_ADJECTIVE_OF = (r"(?!-speaking)(?!\s+(?:market|markets|bank|banks|client|clients|customer|customers|regulation|regulations|"
                        r"regulator|law|laws|legislation|company|companies|entity|entities|branch|subsidiary|franchise|exchange))")
_MENTION_RE = re.compile(rf"\b(?P<german>german{_GERMAN_ADJECTIVE_OF}|deutsch\w*)\b|\b(?P<other>{_LANGUAGE_NAMES})\b", re.IGNORECASE)
_ABOVE_BASIC_RE = re.compile(
    r"\b(?:fluent\w*|(?:very |really |quite |pretty )?good|great|strong|excellent|solid|working|business(?:-level)?|"
    r"conversational|native|proficien\w*|advanced|intermediate|professional|full|high|fair|decent|comfortable|competent|"
    r"bilingual|mother tongue|[ABC]2|B1|C1)\b|\bupper[- ]intermediate\b", re.IGNORECASE)
_ABOVE_BASIC_CASE_RE = re.compile(r"\b(?:A2|B1|B2|C1|C2)\b")       # CEFR codes need upper case: "a2" is not a level
_NEGATION_RE = re.compile(r"\b(?:not|no|never|without|nor|neither)\s+(?:\w+\s+)?$", re.IGNORECASE)
_BASIC_RE = re.compile(r"\b(?:basic|beginner|elementary|rudimentary|A1)\b|\bbasics\b|\bgrundkenntnisse\b|\banf[aä]nger\w*\b", re.IGNORECASE)
_CLAUSE_SPLIT_RE = re.compile(
    r"[.!?;:\n]+|,\s*(?:but|while|whereas|though|although|and|my|i|with|whereby)\b|\b(?:but|while|whereas|although|though)\b|"
    r"\band\s+(?:my|i)\b", re.IGNORECASE)
_CHAIN_GAP_RE = re.compile(r"^(?:\s|,|/|&|\band\b|\bund\b|\bor\b|\boder\b)*$", re.IGNORECASE)
_MAX_LEVEL_GAP = 40         # characters between a level word and the language it describes


def _clauses(text: str) -> list[str]:
    return [c for c in _CLAUSE_SPLIT_RE.split(text) if c and c.strip()]


def _chains(clause: str) -> list[tuple[int, int, bool]]:
    """(start, end, has_german) of the groups of language names joined by 'and', ',', '/', '&' ('English and German')."""
    chains: list[list] = []
    for m in _MENTION_RE.finditer(clause):
        german = m.group("german") is not None
        if chains and _CHAIN_GAP_RE.match(clause[chains[-1][1]:m.start()]):
            chains[-1][1] = m.end()
            chains[-1][2] = chains[-1][2] or german
        else:
            chains.append([m.start(), m.end(), german])
    return [(a, b, g) for a, b, g in chains]


def _gap(start: int, end: int, c_start: int, c_end: int) -> int:
    return max(c_start - end, start - c_end, 0)


def _above_basic_german_in(clause: str) -> str | None:
    """The level word of the clause that describes German, or None. A level word describes the nearest group of language
    names (within 40 characters), and a tie goes to German; a negated word ("not fluent") is ignored."""
    chains = _chains(clause)
    if not any(g for _a, _b, g in chains):
        return None
    words = [(m.start(), m.end(), m.group(0)) for m in _ABOVE_BASIC_RE.finditer(clause)
             if m.group(0).lower() not in ("a2", "b1", "c1", "b2", "c2") or _ABOVE_BASIC_CASE_RE.fullmatch(m.group(0))]
    for start, end, word in words:
        if _NEGATION_RE.search(clause[max(0, start - 14):start]):
            continue
        best = min(chains, key=lambda c: (_gap(start, end, c[0], c[1]), not c[2]))
        if _gap(start, end, best[0], best[1]) <= _MAX_LEVEL_GAP and best[2]:
            return word
    return None


_SENTENCE_SPLIT_RE = re.compile(r"[.!?;\n]+")
_LIST_FILLER = r"(?:(?:in|of|at|with|and|to|both|all|each|my|the|are|is|languages?|knowledge|skills?)\b\s*)*"
_LIST_BEFORE_RE = re.compile(rf"\s*{_LIST_FILLER}", re.IGNORECASE)
_LIST_AFTER_RE = re.compile(r"^[\s,:()\-–—]*(?:(?:all|both|each|are|is|at|to|my|languages?)\b[\s,:()\-]*)*", re.IGNORECASE)


def _level_word_ok(m: re.Match) -> bool:
    word = m.group(0)
    return word.lower() not in ("a2", "b1", "c1", "b2", "c2") or bool(_ABOVE_BASIC_CASE_RE.fullmatch(word))


def _above_basic_german_in_list(sentence: str) -> str | None:
    """A list of at least two languages that includes German, with one level word for the whole list: before it
    ("fluent in English, Polish and German") or after it ("English, Polish and German: all fluent", "English, German
    and Polish (fluent)"). The level word must sit right at the list (only filler words between), so
    "English and Polish (fluent), German (basic)" and "fluent in English and Polish; basic German" are not flagged."""
    for start, end, has_german in _chains(sentence):
        if not has_german or len(list(_MENTION_RE.finditer(sentence, start, end))) < 2:
            continue
        before = sentence[max(0, start - 40):start]
        for m in _ABOVE_BASIC_RE.finditer(before):
            if (_level_word_ok(m) and _LIST_BEFORE_RE.fullmatch(before[m.end():])
                    and not _NEGATION_RE.search(before[max(0, m.start() - 14):m.start()])):
                return m.group(0)
        after = sentence[end:end + 60]
        lead = _LIST_AFTER_RE.match(after)
        m = _ABOVE_BASIC_RE.match(after, lead.end())
        if m and _level_word_ok(m):
            return m.group(0)
    return None


def _language_claim_findings(parts: list[tuple[str, str]]) -> list[dict]:
    """`language_claim`: text that gives the user's German a level above basic. `parts` are (where, text)."""
    out = []
    for where, text in parts:
        for clause in _clauses(text):
            word = _above_basic_german_in(clause)
            if word:
                out.append(_finding("language_claim", clause.strip()[:140], where,
                                    f"'{word}' next to German: the facts say your German is basic and no document may claim more",
                                    clause))
                break
        else:
            for sentence in _SENTENCE_SPLIT_RE.split(text):
                word = _above_basic_german_in_list(sentence)
                if word:
                    out.append(_finding("language_claim", sentence.strip()[:140], where,
                                        f"'{word}' for a list of languages that includes German: the facts say your German is basic and no document may claim more",
                                        sentence))
                    break
    return out


# Only filler may sit between the basic word and the language names it describes ("basic knowledge of German",
# "German is a plus" must not count: the basic word there belongs to something else).
_BASIC_BEFORE_RE = re.compile(r"^\s*(?:(?:knowledge|command|skills?|understanding|level|grasp)\s+)?(?:(?:of|in)\s+)?$", re.IGNORECASE)
_BASIC_AFTER_RE = re.compile(r"^[\s,:()\-–—]*(?:(?:is|are|at|a|an|only|still|my|skills?|knowledge|level)\b[\s,:()\-–—]*)*$", re.IGNORECASE)


def _states_basic_german(text: str) -> bool:
    """True if one clause of the text names German and says it is basic (a beginner level): the basic word must describe
    German (only filler between them), not stand next to something else, and must not be negated."""
    for clause in _clauses(text):
        chains = [c for c in _chains(clause) if c[2]]
        for m in _BASIC_RE.finditer(clause):
            if _NEGATION_RE.search(clause[max(0, m.start() - 14):m.start()]):
                continue
            for start, end, _g in chains:
                if m.end() <= start and _BASIC_BEFORE_RE.match(clause[m.end():start]):
                    return True
                if m.start() >= end and _BASIC_AFTER_RE.match(clause[end:m.start()]):
                    return True
    return False


def german_required(conn, job_id: int, title: str, description: str | None, cfg: dict) -> bool:
    """Does the job require German? True when the language filter of the job's latest check recorded a German requirement
    (`language_c1_required`, `language_required_below_c1`, `language_level_unspecified` for German, or
    `language_implied_by_posting`), or when reading the posting again finds one (a plus does not count)."""
    last = conn.execute("SELECT MAX(id) FROM job_filter_results WHERE job_id = ? AND filter = 'language'", (job_id,)).fetchone()[0]
    if last:
        rows = conn.execute(
            "SELECT reason_code, comment FROM job_filter_results WHERE job_id = ? AND filter = 'language' AND created_at = "
            "(SELECT created_at FROM job_filter_results WHERE id = ?)", (job_id, last))
        for r in rows:
            code, comment = r["reason_code"], r["comment"] or ""
            if code == "language_implied_by_posting":
                return True
            if code in ("language_c1_required", "language_required_below_c1", "language_level_unspecified") \
                    and comment.lower().startswith("german"):
                return True
    text = f"{title}\n{description or ''}"
    return any(r.language.lower() == "german" and r.code != "language_optional" for r in language_filter.assess(text, cfg))


# ----------------------------------------------------------------------------- storing

@dataclass
class DocumentOutcome:
    stored: list[tuple[int, str, str, int]] = field(default_factory=list)    # job id, kind, status, number of findings
    ignored: list[tuple[int, str]] = field(default_factory=list)             # job id, reason
    missing: list[tuple[int, list[str]]] = field(default_factory=list)       # job id, kinds still missing
    frozen: list[tuple[int, str]] = field(default_factory=list)              # job id, kind: approved already, the new text was not stored
    rendered: "RenderOutcome | None" = None


def store_document(conn, job_id: int, kind: str, run_id: str, content: dict, findings: list[dict],
                   generated_on: date, stamp: str) -> str:
    status = "blocked" if has_blocking(findings) else "draft"
    conn.execute("DELETE FROM finding_overrides WHERE document_id IN (SELECT id FROM documents WHERE job_id = ? AND kind = ?)", (job_id, kind))
    conn.execute(
        """INSERT INTO documents (job_id, kind, run_id, import_id, content, status, findings, generated_on, created_at)
           VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)
           ON CONFLICT(job_id, kind) DO UPDATE SET run_id = excluded.run_id, import_id = NULL, content = excluded.content,
               status = excluded.status, findings = excluded.findings, generated_on = excluded.generated_on,
               created_at = excluded.created_at, pdf_path = NULL, pages = NULL, rendered_at = NULL,
               approved_at = NULL, approved_pdf_path = NULL""",
        (job_id, kind, run_id, json.dumps(content, ensure_ascii=False), status,
         json.dumps(findings, ensure_ascii=False), generated_on.isoformat(), stamp))
    return status


def import_documents(conn, cfg: dict, batch_id: str, results: list[dict], generated_on: date, stamp: str) -> DocumentOutcome:
    """Handle the `documents` of the score records of a results file (the scores are stored and the selection is
    recomputed already). Jobs that do not qualify are ignored and listed; qualifying jobs without documents are listed."""
    out = DocumentOutcome()
    eligible = eligible_jobs(conn, batch_id, cfg)
    pending = [e for e in results if e.get("documents")]
    if pending:
        facts, index = tr.load_facts(), load_fact_index()
        home = cfg["location"].get("car_origin_address") or ""
    with conn:
        for entry in pending:
            job_id = entry["job_id"]
            docs = dict(entry["documents"])
            for kind in KINDS:                              # an approved document is frozen (decided by the user, 1 Nov 2026)
                row = conn.execute("SELECT status FROM documents WHERE job_id = ? AND kind = ?", (job_id, kind)).fetchone()
                if kind in docs and row is not None and row["status"] == "approved":
                    del docs[kind]
                    out.frozen.append((job_id, kind))
            if not docs:
                continue
            if job_id not in eligible:
                out.ignored.append((job_id, not_eligible_reason(conn, batch_id, job_id, cfg)))
                continue
            job_row = conn.execute("SELECT title, company, description FROM jobs WHERE id = ?", (job_id,)).fetchone()
            job = {"title": job_row["title"], "company": job_row["company"], "description": job_row["description"],
                   "locations": [r["location_text"] for r in conn.execute(
                       "SELECT location_text FROM job_locations WHERE job_id = ? ORDER BY id", (job_id,))]}
            for kind in KINDS:
                if kind not in docs:
                    continue
                findings = check_content(kind, docs[kind], job, facts, index, generated_on, home,
                                         kind == "cover_letter" and german_required(conn, job_id, job["title"], job["description"], cfg))
                old = conn.execute("SELECT pdf_path FROM documents WHERE job_id = ? AND kind = ?", (job_id, kind)).fetchone()
                status = store_document(conn, job_id, kind, batch_id, docs[kind], findings, generated_on, stamp)
                if old and old["pdf_path"]:
                    discard_pdf(cfg, old["pdf_path"])               # the text changed; the old file would be stale
                out.stored.append((job_id, kind, status, len(findings)))
    if out.stored:
        out.rendered = render_stored(conn, cfg, sorted({j for j, _k, _s, _n in out.stored}), stamp=stamp)
    for job_id in eligible:
        have = {r["kind"] for r in conn.execute("SELECT kind FROM documents WHERE job_id = ?", (job_id,))}
        lacking = [k for k in KINDS if k not in have]
        if lacking:
            out.missing.append((job_id, lacking))
    return out


@dataclass
class RevisionStored:
    status: str                                  # stored | stale | failed | ignored (no such request, or not relayed any more)
    outcome: str = ""                            # draft | blocked (stored); invalid (failed)
    message: str = ""
    job_id: int | None = None
    kind: str = ""
    findings: list[dict] = field(default_factory=list)
    rendered: "RenderOutcome | None" = None


def _revision_marker_findings(content: dict) -> list[dict]:
    if any(ADDRESS_MARKER in s for s in _strings(content)):
        return [_finding("address", "(home address)", "document",
                         f"the text still holds the marker {ADDRESS_MARKER}: the home address must never be in a document, so the marker "
                         "must be rewritten away")]
    return []


def finish_revision_request(conn, request_id: int, status: str, outcome: str, message: str, stamp: str, previous: str | None = None) -> None:
    conn.execute("UPDATE revision_requests SET status = ?, outcome = ?, message = ?, finished_at = ?, "
                 "previous_content = COALESCE(?, previous_content) WHERE id = ?", (status, outcome, message[:300], stamp, previous, request_id))


def store_revision(conn, cfg: dict, request_id: int, content: dict, attention=(), stamp: str | None = None,
                   today: date | None = None) -> RevisionStored:
    """Store the revised document of a relayed request (v0.85.0). Stale (nothing stored) unless the document is still blocked with the
    exact text the request was made for. The revision goes through the checks of an import (plus the marker check): a clean one is a
    draft and gets its PDF, one that still has findings is stored as blocked with the new findings. A schema-invalid document leaves the
    document alone and fails the request. The accepted overrides stay (decided 12 Nov 2026, D3); the replaced text is kept in the request."""
    from zoneinfo import ZoneInfo
    from datetime import datetime
    from jsonschema import Draft202012Validator
    from . import schemas
    stamp = stamp or db.utcnow()
    today = today or datetime.now(ZoneInfo(cfg["timezone"])).date()
    conn.execute("BEGIN IMMEDIATE")
    try:
        req = conn.execute("SELECT * FROM revision_requests WHERE id = ?", (request_id,)).fetchone()
        if req is None or req["status"] != "relayed":
            return RevisionStored("ignored")
        kind = req["kind"]
        doc = conn.execute("SELECT d.*, j.title, j.company, j.description FROM documents d JOIN jobs j ON j.id = d.job_id "
                           "WHERE d.id = ?", (req["document_id"],)).fetchone()
        if doc is None or not revisable(doc) or content_sha(doc["content"]) != req["content_sha"]:
            finish_revision_request(conn, request_id, "stale", "", "the document changed while the writer worked; nothing was stored", stamp)
            conn.commit()
            return RevisionStored("stale", message="the document changed while the writer worked; nothing was stored",
                                  job_id=doc["job_id"] if doc else None, kind=kind)
        schema = schemas.COVER_LETTER if kind == "cover_letter" else schemas.CV
        bad = [f"{'/'.join(map(str, e.path)) or 'document'}: {schemas.message(e)}" for e in Draft202012Validator(schema).iter_errors(content)]
        if bad:
            msg = ("the revised document is not valid: " + "; ".join(bad[:3]))[:300]
            finish_revision_request(conn, request_id, "failed", "invalid", msg, stamp)
            conn.commit()
            return RevisionStored("failed", "invalid", msg, doc["job_id"], kind)
        job = {"title": doc["title"], "company": doc["company"], "description": doc["description"],
               "locations": [r["location_text"] for r in conn.execute(
                   "SELECT location_text FROM job_locations WHERE job_id = ? ORDER BY id", (doc["job_id"],))]}
        home = cfg["location"].get("car_origin_address") or ""
        findings = check_content(kind, content, job, tr.load_facts(), load_fact_index(), today, home,
                                 kind == "cover_letter" and german_required(conn, doc["job_id"], job["title"], job["description"], cfg))
        findings += _revision_marker_findings(content)
        status = "blocked" if has_blocking(findings, load_overrides(conn, doc["id"])) else "draft"
        done = conn.execute(
            "UPDATE documents SET content = ?, status = ?, findings = ?, generated_on = ?, pdf_path = NULL, pages = NULL, rendered_at = NULL "
            "WHERE id = ? AND status = 'blocked' AND content = ?",
            (json.dumps(content, ensure_ascii=False), status, json.dumps(findings, ensure_ascii=False), today.isoformat(), doc["id"], doc["content"])).rowcount
        if not done:
            conn.rollback()
            conn.execute("BEGIN IMMEDIATE")
            finish_revision_request(conn, request_id, "stale", "", "the document changed while the writer worked; nothing was stored", stamp)
            conn.commit()
            return RevisionStored("stale", message="the document changed while the writer worked; nothing was stored", job_id=doc["job_id"], kind=kind)
        finish_revision_request(conn, request_id, "stored", status, "", stamp, previous=doc["content"])
        conn.commit()
    finally:
        if conn.in_transaction:
            conn.rollback()
    for note in attention:
        add_review_attention(conn, doc["job_id"], kind, note)
    if doc["pdf_path"]:
        discard_pdf(cfg, doc["pdf_path"])
    out = RevisionStored("stored", status, "", doc["job_id"], kind, findings)
    if status == "draft":
        out.rendered = render_stored(conn, cfg, [doc["job_id"]], stamp=stamp)
    return out


# ----------------------------------------------------------------------------- reading

def state(conn, job_id: int, cfg: dict | None = None) -> tuple[str, int]:
    """('approved' | 'ready' | 'blocked' | 'unrendered' | 'missing', number of findings) for the documents of one job.
    `approved` needs both documents approved (it wins over `ready`). `ready` needs both documents, no blocking findings
    and both PDFs recorded (and, with `cfg`, present on disk); an approved document needs no PDF in `paths.documents`.
    A page-limit warning does not stop `ready` (see `notes`)."""
    rows = conn.execute("SELECT id, kind, status, findings, pdf_path FROM documents WHERE job_id = ?", (job_id,)).fetchall()
    if {r["kind"] for r in rows} != set(KINDS):
        return "missing", 0
    blocked = [r for r in rows if r["status"] == "blocked"]
    if blocked:
        return "blocked", sum(1 for r in blocked for f in json.loads(r["findings"])
                              if f["kind"] not in WARNING_KINDS and not (overridable(f) and finding_key(f) in load_overrides(conn, r["id"])))      # warnings and accepted findings are not counted
    if all(r["status"] == "approved" for r in rows):
        return "approved", 0
    root = documents_dir(cfg) if cfg is not None else None
    if any(r["status"] != "approved" and (not r["pdf_path"] or (root is not None and not (root / r["pdf_path"]).is_file()))
           for r in rows):
        return "unrendered", 0
    return "ready", 0


def notes(conn, job_id: int) -> list[str]:
    """Short warnings of the PDF step for the report, such as 'cover letter is 2 pages (limit 1)'."""
    out: list[str] = []
    for r in conn.execute("SELECT kind, status, findings FROM documents WHERE job_id = ? AND status IN ('draft', 'blocked') ORDER BY kind", (job_id,)):
        if r["status"] == "draft":
            out += [f["reason"] for f in json.loads(r["findings"]) if f["kind"] == pdf.PAGE_WARNING]
            out += [f"{r['kind']} {f['where']}: {f['reason']}" for f in json.loads(r["findings"]) if f["kind"] == pdf.LINE_ENDING_WARNING]
        out += [f"NEEDS ATTENTION: {f['where']}: {f['reason']}" for f in json.loads(r["findings"]) if f["kind"] == REVIEW_ATTENTION]
    return out


def add_review_attention(conn, job_id: int, kind: str, note: str) -> bool:
    """The headless reviewer flagged this draft or blocked document (v0.78.0, blocked since v0.78.9): a finding of kind
    `review_attention`. The status does not change (`state` looks at the status, not at the finding kinds). False when there
    is no such document of that kind or the note is stored."""
    row = conn.execute("SELECT findings FROM documents WHERE job_id = ? AND kind = ? AND status IN ('draft', 'blocked')", (job_id, kind)).fetchone()
    if row is None or kind not in KINDS:
        return False
    findings = json.loads(row["findings"])
    if any(f["kind"] == REVIEW_ATTENTION and f["reason"] == note for f in findings):
        return False
    findings.append(_finding(REVIEW_ATTENTION, "", kind, note))
    with conn:
        conn.execute("UPDATE documents SET findings = ? WHERE job_id = ? AND kind = ?", (json.dumps(findings, ensure_ascii=False), job_id, kind))
    return True


def add_override(conn, job_id: int, kind: str, f_kind: str, f_where: str, f_text: str, comment: str, stamp: str | None = None) -> str | None:
    """Accept one finding of a blocked document as a false positive (v0.83.0). Returns why it is refused, or None when it is
    stored (the caller holds the write transaction). Refused: no such blocked document, no such finding in it, a warning, a
    language or address finding."""
    row = conn.execute("SELECT id, status, findings FROM documents WHERE job_id = ? AND kind = ?", (job_id, kind)).fetchone()
    if row is None or row["status"] != "blocked":
        return f"the {kind.replace('_', ' ')} of job {job_id} is not blocked"
    found = [f for f in json.loads(row["findings"]) if finding_key(f) == (f_kind, f_where, f_text)]
    if not found:
        return "that finding is not in the document any more (it was rechecked); reload the page"
    if not overridable(found[0]):
        return "this kind of finding cannot be accepted: change the text and import a new results file"
    conn.execute("INSERT OR REPLACE INTO finding_overrides (document_id, kind, finding_where, text, comment, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                 (row["id"], f_kind, f_where, f_text, comment, stamp or db.utcnow()))
    return None


def recheck_blocked(conn, cfg: dict, *, apply: bool = False, job_id: int | None = None) -> list[dict]:
    """Run today's checks again on the stored blocked documents (v0.78.11). Without `apply` nothing is written. With it, the
    findings are replaced (the reviewer's flags stay) and a document with no finding left becomes a draft and gets its PDF."""
    sql = ("SELECT d.id, d.job_id, d.kind, d.content, d.findings, d.generated_on, j.title, j.company, j.description "
           "FROM documents d JOIN jobs j ON j.id = d.job_id WHERE d.status = 'blocked'")
    rows = conn.execute(sql + (" AND d.job_id = ?" if job_id else "") + " ORDER BY d.job_id, d.kind", (job_id,) if job_id else ()).fetchall()
    if not rows:
        return []
    facts, index = tr.load_facts(), load_fact_index()
    home = cfg["location"].get("car_origin_address") or ""
    out: list[dict] = []
    for r in rows:
        job = {"title": r["title"], "company": r["company"], "description": r["description"],
               "locations": [x["location_text"] for x in conn.execute(
                   "SELECT location_text FROM job_locations WHERE job_id = ? ORDER BY id", (r["job_id"],))]}
        content, on = json.loads(r["content"]), date.fromisoformat(r["generated_on"])
        findings = check_content(r["kind"], content, job, facts, index, on, home,
                                 r["kind"] == "cover_letter" and german_required(conn, r["job_id"], job["title"], job["description"], cfg))
        old = json.loads(r["findings"])
        kept = [f for f in old if f["kind"] == REVIEW_ATTENTION]
        status = "blocked" if has_blocking(findings, load_overrides(conn, r["id"])) else "draft"
        entry = {"job_id": r["job_id"], "kind": r["kind"], "status": status, "before": len(old) - len(kept), "findings": findings}
        out.append(entry)
        if apply:
            with conn:      # only if the document is still blocked with the content that was checked (a results file may have replaced it)
                done = conn.execute("UPDATE documents SET status = ?, findings = ? WHERE id = ? AND status = 'blocked' AND content = ?",
                                    (status, json.dumps(findings + kept, ensure_ascii=False), r["id"], r["content"])).rowcount
            if not done:
                entry["status"] = "changed"                 # changed meanwhile: nothing was written for it
    cleared = sorted({o["job_id"] for o in out if o["status"] == "draft"})
    if apply and cleared:
        render_stored(conn, cfg, cleared)
    return out


def document_text(kind: str, content: dict) -> str:
    """The session's text of a stored document, for the user to read (`python -m app documents --job ID`)."""
    lines: list[str] = []
    if kind == "cover_letter":
        b = content["blocks"]
        lines += [f"Contact: {content.get('contact_name') or 'none named in the posting (Dear Hiring Manager,)'}", "",
                  b["opening"], "", b["lead_in"]]
        lines += [f"  - {a['label']}: {a['text']}  [{', '.join(a['facts'])}]" for a in b["achievements"]]
        lines += ["", b["skills"], "", b["closing"]]
    else:
        lines += [f"Variant: {content['variant']}", "", content["summary"]]
        for exp in content["experience"]:
            lines += ["", f"{exp['experience_id']}:"] + [f"  - {b['text']}  [{b['fact_id']}]" for b in exp["bullets"]]
        if content.get("skills"):
            lines += ["", "Skill groups: " + ", ".join(content["skills"])]
    return "\n".join(lines)


# ----------------------------------------------------------------------------- PDFs (slices 3c and 3d)

def documents_dir(cfg: dict) -> Path:
    return resolve_path(cfg["paths"]["documents"])


def approved_dir(cfg: dict) -> Path:
    return resolve_path(cfg["paths"]["approved"])


def photo_path(cfg: dict) -> Path | None:
    value = cfg["tailoring"]["photo"].strip()
    return resolve_path(value) if value else None


def load_profile(master_path=None, variants_path=None) -> tuple[dict, dict]:
    """The parsed fact file and variants file (the PDF code reads person, experience, education, skills from them)."""
    master = yaml.safe_load((master_path or ROOT / PROFILE_FILES["master_cv"]).read_text(encoding="utf-8")) or {}
    variants = yaml.safe_load((variants_path or ROOT / PROFILE_FILES["variants"]).read_text(encoding="utf-8")) or {}
    return master, variants


def discard_pdf(cfg: dict, relative: str) -> None:
    """Delete a PDF named in a row, only when it lies inside the documents folder."""
    root = documents_dir(cfg).resolve()
    target = (root / relative).resolve()
    if root in target.parents and target.is_file():
        try:
            target.unlink()
        except OSError:
            log.warning("could not remove the old PDF %s", target)


@dataclass
class RenderOutcome:
    rendered: list[tuple[int, str, str, int]] = field(default_factory=list)    # job id, kind, path (as stored or shown), pages
    over_limit: list[tuple[int, str, int, int]] = field(default_factory=list)  # job id, kind, pages, limit
    failed: list[tuple[int, str, str]] = field(default_factory=list)           # job id, kind, reason
    warnings: list[str] = field(default_factory=list)                          # for example "photo not found"
    skipped: list[tuple[int, str, str]] = field(default_factory=list)          # job id, kind, reason (blocked)
    line_endings: list[tuple[int, str, int]] = field(default_factory=list)     # job id, kind, number of blocks with a stub last line


def render_stored(conn, cfg: dict, job_ids: list[int] | None = None, out_dir: Path | None = None, stamp: str | None = None,
                  write_db: bool = True) -> RenderOutcome:
    """Build the PDFs of stored `draft` documents (all jobs, or `job_ids`).

    Output goes to `paths.documents/<job id>_<company>/` (or `out_dir`). With `write_db` the row gets `pdf_path`
    (relative to the documents folder), `pages` and `rendered_at`, and the warning findings of the PDF step are
    recomputed (an earlier warning disappears when the file now fits). Blocked documents are skipped. Nothing here
    raises for one bad document: it is listed in `failed` and recorded as a `render_error` warning."""
    from .db import utcnow
    out = RenderOutcome()
    stamp = stamp or utcnow()
    root = Path(out_dir) if out_dir else documents_dir(cfg)
    sql = ("SELECT d.id, d.job_id, d.kind, d.content, d.findings, d.status, d.generated_on, j.title, j.company "
           "FROM documents d JOIN jobs j ON j.id = d.job_id")
    params: list = []
    if job_ids is not None:
        sql += f" WHERE d.job_id IN ({','.join('?' * len(job_ids))})"
        params = list(job_ids)
    rows = conn.execute(sql + " ORDER BY d.job_id, d.kind", params).fetchall()
    if not rows:
        return out
    master, variants = load_profile()
    tl = cfg["tailoring"]
    home = cfg["location"].get("car_origin_address") or ""
    photo = photo_path(cfg)
    photo_warned = False
    for r in rows:
        if r["status"] == "approved":
            out.skipped.append((r["job_id"], r["kind"], "approved, frozen (undo the approval to build it again)"))
            continue
        if r["status"] != "draft":
            out.skipped.append((r["job_id"], r["kind"], "blocked by the checks, no PDF"))
            continue
        content = json.loads(r["content"])
        locations = [x["location_text"] for x in conn.execute(
            "SELECT location_text FROM job_locations WHERE job_id = ? ORDER BY id", (r["job_id"],))]
        job = {"title": r["title"], "company": r["company"], "locations": locations}
        target = root / pdf.folder_name(r["job_id"], r["company"]) / pdf.file_name(master["person"]["name"], r["kind"], r["company"])
        generated_on = date.fromisoformat(r["generated_on"])
        findings = [f for f in json.loads(r["findings"]) if f["kind"] not in PDF_WARNING_KINDS]
        try:
            if r["kind"] == "cover_letter":
                res = pdf.render_cover_letter(content, job, master, generated_on, target, home)
                limit, label = tl["letter_max_pages"], "cover letter"
            else:
                res = pdf.render_cv(content, master, variants, target, photo, generated_on, home)
                limit, label = tl["cv_max_pages"], "CV"
        except Exception as exc:                      # one document must not stop the import or the others
            reason = f"{type(exc).__name__}: {exc}"
            out.failed.append((r["job_id"], r["kind"], reason))
            log.warning("PDF for job %s %s failed: %s", r["job_id"], r["kind"], reason)
            if write_db:
                findings.append({"kind": "render_error", "text": reason[:140], "block": None, "where": "pdf",
                                 "reason": f"the PDF could not be built: {reason}"[:300], "block_text": ""})
                with conn:
                    conn.execute("UPDATE documents SET findings = ?, pdf_path = NULL, pages = NULL, rendered_at = NULL WHERE id = ?",
                                 (json.dumps(findings, ensure_ascii=False), r["id"]))
            continue
        for w in res.warnings:
            if not photo_warned:
                out.warnings.append(w)
                photo_warned = True
        if res.pages > limit:
            out.over_limit.append((r["job_id"], r["kind"], res.pages, limit))
            findings.append({"kind": pdf.PAGE_WARNING, "text": f"{res.pages} pages", "block": None, "where": "pdf",
                             "reason": f"{label} is {res.pages} pages (limit {limit})", "block_text": ""})
        flags = pdf.document_line_flags(r["kind"], content)         # warnings only: a stub last line never blocks a document
        if flags:
            out.line_endings.append((r["job_id"], r["kind"], len(flags)))
            findings += [{"kind": pdf.LINE_ENDING_WARNING, "text": f["last_line"][:140], "block": None, "where": f["location"],
                          "reason": pdf.line_ending_reason(f), "block_text": ""} for f in flags]
        relative = target.relative_to(root).as_posix()
        out.rendered.append((r["job_id"], r["kind"], relative if write_db else str(target), res.pages))
        if write_db:
            with conn:
                conn.execute("UPDATE documents SET pdf_path = ?, pages = ?, rendered_at = ?, findings = ? WHERE id = ?",
                             (relative, res.pages, stamp, json.dumps(findings, ensure_ascii=False), r["id"]))
    return out
