"""Traceability check (goal.md 5.6): every employer, title, date, number, skill and certificate in a
generated document must exist in the fact files. A failed check blocks the document.

Input: a `Document` (a title line plus text blocks, each block optionally citing fact ids) and the
`Facts` loaded from `profile/master_cv.yaml` (and the summaries and headlines of `profile/variants.yaml`).
Output: a `TraceResult` with `ok` and a list of `Finding`s (kind, offending text, block, reason).

The check is deliberately conservative: it prefers a false alarm to a missed fabrication. What it can
recognise is data in `app/traceability_vocab.yaml`; the limits are listed in goal.md 5.6. Pure Python,
no network, no database.
"""
from __future__ import annotations

import json
from datetime import date
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml

from .config import ROOT

VOCAB_PATH = Path(__file__).with_name("traceability_vocab.yaml")
DEFAULT_MASTER = ROOT / "profile" / "master_cv.yaml"
DEFAULT_VARIANTS = ROOT / "profile" / "variants.yaml"

KINDS = ("employer", "title", "date", "number", "skill", "certificate", "unknown_fact_id")


class TraceabilityError(Exception):
    """The document or a fact file could not be read."""


# ----------------------------------------------------------------------------- text helpers

_TRANSLATE = str.maketrans({"ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ß": "ss", "æ": "ae", "Æ": "AE",
                            "œ": "oe", "ı": "i", "’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"})
_CLEAN = str.maketrans({" ": " ", " ": " ", " ": " ", "’": "'", "‘": "'", "“": '"', "”": '"'})


def fold(s: str) -> str:
    """Lower case without accents: ä ö ü ł é become a o u l e; 'Zürich' == 'zurich'."""
    s = unicodedata.normalize("NFKD", s.translate(_TRANSLATE))
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


def words(s: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9+#]+", fold(s)))


def _clean(text: str) -> str:
    """Length-preserving clean-up (no-break spaces, curly quotes)."""
    return text.translate(_CLEAN)


_SOURCE_NOTE = re.compile(r"\s*\((?:resume|resume_cs|cover_letter)(?:\s+only)?\)", re.I)


def strip_source_notes(s: str) -> str:
    """'SQL Server Reporting Services (resume_cs only)' -> 'SQL Server Reporting Services'."""
    return _SOURCE_NOTE.sub("", s).strip()


@dataclass(frozen=True)
class Tok:
    text: str
    start: int
    end: int

    @property
    def cap(self) -> bool:
        return self.text[0].isalpha() and self.text[0].isupper()


_TOKEN = re.compile(r"[&/@]|\w+(?:[-']\w+)*")


def tokenize(text: str) -> list[Tok]:
    return [Tok(m.group(), m.start(), m.end()) for m in _TOKEN.finditer(text)]


_CONNECT = {"of", "de", "von", "van", "du", "la", "le", "and", "&"}


def _contiguous(text: str, a: Tok, b: Tok) -> bool:
    return text[a.end:b.start].strip() == ""


# Capitalised words that start the next clause, never part of a name: "at UBS I built", "at Contoso We".
_RUN_STOP = {"i", "we", "my", "our", "it", "he", "she", "they", "their", "his", "her", "this", "that", "these", "those",
             "the", "a", "an", "in", "on", "at", "as", "for", "with", "to", "from", "by", "after", "before", "during",
             "while", "when", "where", "there", "here", "if", "but", "so", "then", "also", "today", "currently", "now",
             "since", "over", "through", "within", "using", "building", "working"}


def _stops_run(tok: Tok) -> bool:
    return tok.text.split("'")[0].lower() in _RUN_STOP


def cap_run(text: str, toks: list[Tok], i: int) -> int:
    """End index (exclusive) of the run of capitalised tokens that starts at toks[i]."""
    j = i + 1
    while j < len(toks) and _contiguous(text, toks[j - 1], toks[j]):
        if toks[j].cap and not _stops_run(toks[j]):
            j += 1
        elif toks[j].text.lower() in _CONNECT and j + 1 < len(toks) and toks[j + 1].cap and not _stops_run(toks[j + 1]) and _contiguous(text, toks[j], toks[j + 1]):
            j += 2
        else:
            break
    return j


# ----------------------------------------------------------------------------- numbers and dates

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_MONTH_PAT = r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
_YEAR = r"(?:19|20)\d{2}"
_CUR_PRE = r"(?:CHF|USD|EUR|GBP|PLN|SFr\.?|Fr\.|[$€£]|zł)"
_CUR_POST = r"(?:(?:CHF|USD|EUR|GBP|PLN|SFr|euros?|dollars?|francs?)\b|Fr\.|zł|[$€£])"
_NUM = (r"\d{1,3}(?:['   ]\d{3})+(?:[.,]\d+)?"
        r"|\d{1,3}(?:,\d{3})+(?:\.\d+)?"
        r"|\d{1,3}(?:\.\d{3})+(?!\d)"
        r"|\d+(?:[.,]\d+)?")
_MULT = r"(?:\s?(?:million|billion|thousand|mn|bn)\b|[kKmM]\b)"
_NUMBER = re.compile(rf"(?<![\w.,'])(?:(?P<pre>{_CUR_PRE})\s?)?(?P<num>{_NUM})(?P<mult>{_MULT})?"
                     rf"(?:\s?(?P<post>%|percent\b|per\s?cent\b|{_CUR_POST}))?", re.I)
_MULTIPLIER = {"k": 1_000, "thousand": 1_000, "m": 1_000_000, "mn": 1_000_000, "million": 1_000_000, "bn": 10 ** 9, "billion": 10 ** 9}
_CURRENCY = {"chf": "CHF", "fr.": "CHF", "sfr": "CHF", "sfr.": "CHF", "franc": "CHF", "francs": "CHF", "usd": "USD", "$": "USD",
             "dollar": "USD", "dollars": "USD", "eur": "EUR", "€": "EUR", "euro": "EUR", "euros": "EUR", "gbp": "GBP", "£": "GBP",
             "pln": "PLN", "zł": "PLN"}
_CODE = re.compile(r"\b(?=[A-Z0-9-]*[A-Z])(?=[A-Z0-9-]*\d)[A-Z0-9]+(?:-[A-Z0-9]+)+\b")
_NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
                 "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
                 "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
                 "ninety": 90, "hundred": 100, "thousand": 1000, "million": 10 ** 6, "billion": 10 ** 9, "dozen": 12}
_UNITS = (r"years?|months?|hours?|days?|weeks?|people|percent|per\s?cent|times|countries|teams|projects|systems|users|employees|"
          r"buildings|stakeholders|reports|scripts|tools|FTEs?|colleagues|clients|customers|markets|languages|decades")
_WORD_NUMBER = re.compile(rf"\b(?P<w>{'|'.join(_NUMBER_WORDS)})(?:-(?P<w2>two|three|four|five|six|seven|eight|nine))?\s+(?:{_UNITS})\b", re.I)


@dataclass(frozen=True)
class Num:
    text: str
    value: Decimal
    kind: str                # plain | pct | money
    cur: str | None = None


@dataclass(frozen=True)
class Dated:
    text: str
    year: int
    month: int | None = None


def parse_number(s: str) -> Decimal:
    """'200,000' '200'000' '200 000' '200.000' '2,5' '1.5' -> Decimal. A dot followed by exactly three digits is a thousand separator."""
    s = s.replace("'", "").replace(" ", "").replace(" ", "")
    if re.fullmatch(r"\d{1,3}(?: \d{3})+(?:[.,]\d+)?", s):
        s = s.replace(" ", "")
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", s):
        s = s.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s) and not s.startswith("0."):
        s = s.replace(".", "")
    return Decimal(s.replace(",", "."))


def _blank(text: str, span: tuple[int, int]) -> str:
    return text[:span[0]] + " " * (span[1] - span[0]) + text[span[1]:]


def extract_figures(text: str) -> tuple[list[Num], list[Dated], list[str]]:
    """Numbers, dates (years or year+month) and code tokens (like 1Z0-071) found in a text."""
    text = _clean(text)
    dates: list[Dated] = []
    codes: list[str] = []

    for m in _CODE.finditer(text):
        codes.append(m.group())
    text = _CODE.sub(lambda m: " " * len(m.group()), text)

    def month_of(name: str) -> int:
        return _MONTHS[name[:3].lower()]

    def take(pattern: str, handler, flags=0):
        nonlocal text
        for m in list(re.finditer(pattern, text, flags)):
            handler(m)
            text = _blank(text, m.span())

    take(rf"(?<![\d.])(?P<y>{_YEAR})-(?P<m>0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])(?!\d)",
         lambda m: dates.append(Dated(m.group(), int(m["y"]), int(m["m"]))))
    take(rf"(?<![\d.])(?:0?[1-9]|[12]\d|3[01])\.(?P<m>0?[1-9]|1[0-2])\.(?P<y>{_YEAR})(?!\d)",
         lambda m: dates.append(Dated(m.group(), int(m["y"]), int(m["m"]))))
    take(rf"\b(?:\d{{1,2}}(?:st|nd|rd|th)?\.?\s+)?(?P<mon>{_MONTH_PAT})\b\.?,?\s+(?:\d{{1,2}}(?:st|nd|rd|th)?,?\s+)?(?P<y>{_YEAR})\b",
         lambda m: dates.append(Dated(m.group().strip(), int(m["y"]), month_of(m["mon"]))), re.I)
    take(rf"(?<![\d.])(?P<m>\d{{1,2}})[/.](?P<y>{_YEAR})(?!\d)",
         lambda m: dates.append(Dated(m.group(), int(m["y"]), int(m["m"]))) if 1 <= int(m["m"]) <= 12 else None)
    take(rf"(?<![\d.])(?P<y>{_YEAR})[-/.](?P<m>0?[1-9]|1[0-2])(?![\d])",
         lambda m: dates.append(Dated(m.group(), int(m["y"]), int(m["m"]))))

    def short_range(m):
        y1, yy = int(m["y1"]), int(m["y2"])
        y2 = y1 // 100 * 100 + yy
        if y2 > y1:
            dates.append(Dated(m["y1"], y1))
            dates.append(Dated(m.group(), y2))
    take(rf"(?<![\d.,])(?P<y1>{_YEAR})\s*[-–]\s*(?P<y2>\d{{2}})(?![\d])", short_range)

    # years that are really amounts stay in the text for the number scan
    spans = [m for m in re.finditer(rf"(?<![\w.,'$€£])(?P<y>{_YEAR})(?![\d])", text)]
    for m in spans:
        before = text[:m.start()].rstrip()
        after = text[m.end():]
        if re.search(_CUR_PRE + r"$", before, re.I) or re.match(r"\s?(?:%|[kKmM]\b|million|billion|thousand|CHF|USD|EUR|hours?|FTE|users|people|employees|buildings|percent)", after, re.I) or re.match(r"[.,]\d", after):
            continue
        dates.append(Dated(m.group(), int(m.group())))
        text = _blank(text, m.span())

    nums: list[Num] = []
    ends: list[tuple[int, int]] = []
    for m in _NUMBER.finditer(text):
        nxt = text[m.end():m.end() + 1]
        if not m["mult"] and not m["post"] and nxt and (nxt.isalnum() or nxt == "_"):
            continue                                  # 3D, 5x, 1Z0: part of a word, not a figure
        value = parse_number(m["num"])
        if m["mult"]:
            value *= _MULTIPLIER[m["mult"].strip().lower()]
        kind, cur = "plain", None
        post = (m["post"] or "").lower()
        if m["pre"]:
            kind, cur = "money", _CURRENCY[m["pre"].lower().rstrip(".") if m["pre"].lower() != "fr." else "fr."]
        elif post in ("%", "percent") or post.startswith("per"):
            kind = "pct"
        elif post:
            kind, cur = "money", _CURRENCY[post]
        nums.append(Num(m.group().strip(), value, kind, cur))
        ends.append(m.span())
    for k in range(len(nums) - 1):                     # a range "50-60%" gives the first number the unit of the second
        between = text[ends[k][1]:ends[k + 1][0]]
        if nums[k].kind == "plain" and nums[k + 1].kind != "plain" and re.fullmatch(r"\s*[-–—]\s*|\s+to\s+", between):
            nums[k] = Num(nums[k].text, nums[k].value, nums[k + 1].kind, nums[k + 1].cur)
    for m in _WORD_NUMBER.finditer(text):
        value = _NUMBER_WORDS[m["w"].lower()] + (_NUMBER_WORDS[m["w2"].lower()] if m["w2"] else 0)
        nums.append(Num(m.group(), Decimal(value), "plain"))
    return nums, dates, codes


# ----------------------------------------------------------------------------- vocabulary and facts

def load_vocab(path: Path | None = None) -> dict:
    try:
        data = yaml.safe_load((path or VOCAB_PATH).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise TraceabilityError(f"cannot read the vocabulary file: {exc}") from exc
    return data


def _term_regex(term: str, case_sensitive: bool) -> re.Pattern:
    body = re.escape(term).replace(r"\ ", r"\s+")
    return re.compile(r"(?<![\w+#])" + body + r"(?![\w+#])", 0 if case_sensitive else re.I)


def _ym(value) -> tuple[int, int | None] | None:
    if value is None or str(value).lower() == "present":
        return None
    m = re.match(r"(\d{4})(?:-(\d{1,2}))?", str(value))
    return (int(m[1]), int(m[2]) if m[2] else None) if m else None


def _split_alternatives(s: str) -> list[str]:
    s = strip_source_notes(s)
    return [p.strip() for p in re.split(r"\s+/\s+|\s+for\s+", s) if p.strip()] or [s]


class Facts:
    """The fact files in the form the check needs. Build with `load_facts`."""

    def __init__(self, master: dict, variants: dict | None = None, vocab: dict | None = None):
        self.vocab = vocab if vocab is not None else load_vocab()
        v = self.vocab
        self.ids: set[str] = set()
        self.org_entries: list[tuple[tuple[str, ...], str]] = []        # (words, canonical key)
        self.titles_by_key: dict[str, set[tuple[str, ...]]] = {}
        self.all_titles: set[tuple[str, ...]] = set()
        self.years: set[int] = set()
        self.year_months: set[tuple[int, int]] = set()
        self.values: set[Decimal] = set()
        self.pct: set[Decimal] = set()
        self.money: set[tuple[Decimal, str]] = set()
        self.certs: list[dict] = []
        self.literals: list[str] = []
        self.cert_fact_ids: set[str] = set()
        self.locations = {fold(x) for x in v.get("locations", [])}
        self.legal = {x.lower() for x in v.get("legal_suffixes", [])}
        parts: list[str] = []                                            # everything the corpus is made of

        def walk_ids(node):
            if isinstance(node, dict):
                if isinstance(node.get("id"), str):
                    self.ids.add(node["id"])
                for x in node.values():
                    walk_ids(x)
            elif isinstance(node, list):
                for x in node:
                    walk_ids(x)
        walk_ids(master)

        aliases = {fold(k): list(vals) for k, vals in (v.get("employer_aliases") or {}).items()}
        person = master.get("person") or {}
        for key in ("name", "location"):
            if person.get(key):
                parts.append(str(person[key]))
        for key in ("phone", "email", "linkedin"):
            if person.get(key):
                self.literals.append(str(person[key]))

        def add_org(name: str, key: str):
            w = self._org_words(name)
            if w:
                self.org_entries.append((w, key))

        def add_title(key: str | None, title: str):
            for alt in [title] + [p for p in re.split(r"\s*/\s*", title) if p]:
                w = words(alt)
                if w:
                    self.all_titles.add(w)
                    if key:
                        self.titles_by_key.setdefault(key, set()).add(w)

        for exp in master.get("experience", []):
            title = exp.get("title")
            employer = exp.get("employer")
            if title:
                parts.append(title)
            if exp.get("location"):
                parts.append(exp["location"])
            key = None
            if employer:
                key = fold(employer)
                parts.append(employer)
                for name in {employer, *_split_alternatives(employer), *aliases.get(key, [])}:
                    add_org(name, key)
            if title:
                add_title(key, title)
            for alias in exp.get("title_aliases") or []:           # true descriptions of the role; never printed as its title
                add_title(key, str(alias))
            for field_name in ("start", "end"):
                ym = _ym(exp.get(field_name))
                if ym:
                    self._add_date(*ym)
            for b in exp.get("bullets", []):
                for s in (b.get("skills") or []):
                    parts.append(strip_source_notes(str(s)))
                for s in (b.get("texts") or {}).values():
                    parts.append(s)
                    self._add_figures(s)
                    if re.search(r"certif", s, re.I):
                        self.cert_fact_ids.add(b["id"])
                for s in (b.get("numbers") or []):
                    self._add_figures(str(s))
        for edu in master.get("education", []):
            for k in ("institution", "qualification", "location"):
                if edu.get(k):
                    parts.append(edu[k])
            if edu.get("institution"):
                add_org(edu["institution"], fold(edu["institution"]))
            for field_name in ("start", "end"):
                ym = _ym(edu.get(field_name))
                if ym:
                    self._add_date(*ym)
        for cert in master.get("certifications", []):
            name = strip_source_notes(str(cert.get("name", "")))
            parts.append(name)
            self.certs.append({"name": name, "status": cert.get("status", ""), "id": cert.get("id")})
        for group in master.get("skills", []):
            parts.append(str(group.get("group", "")))
            for item in group.get("items", []):
                for alt in _split_alternatives(str(item)):
                    parts.append(alt)
        parts += [str(term) for term in master.get("known_terms") or []]      # confirmed by the user, v0.78.11
        for lang in master.get("languages", []):
            parts.append(str(lang.get("language", "")))
        for var in ((variants or {}).get("variants") or {}).values():
            for k in ("summary", "headline"):
                if var.get(k):
                    text = " ".join(str(var[k]).split())
                    parts.append(text)
                    self._add_figures(text)
            if var.get("headline"):
                add_title(None, str(var["headline"]).split("|")[0].strip())
        self.corpus_raw = " \n ".join(parts)
        self.corpus = fold(self.corpus_raw)
        self.corpus_words = set(re.findall(r"[a-z0-9+#]+", self.corpus))

    # -- organisation names
    def _org_words(self, name: str) -> tuple[str, ...]:
        return tuple(w for w in words(name) if w not in self.legal)

    def _add_date(self, year: int, month: int | None):
        self.years.add(year)
        if month:
            self.year_months.add((year, month))

    def _add_figures(self, text: str):
        nums, dates, _ = extract_figures(text)
        for n in nums:
            self.values.add(n.value)
            if n.kind == "pct":
                self.pct.add(n.value)
            elif n.kind == "money":
                self.money.add((n.value, n.cur))
        for d in dates:
            self._add_date(d.year, d.month)

    def match_org(self, candidate: str, extra: list[tuple[str, ...]]) -> str | None:
        """'fact:<key>' for a fact-file employer or institution, 'target' for an allowed extra, None if unknown."""
        w = self._org_words(candidate)
        if not w:
            return "target"
        for ew, key in self.org_entries:
            if ew == w:
                return "fact:" + key
        if any(e[:len(w)] == w for e in extra):            # "Helvetia" for the target "Helvetia Versicherungen AG"
            return "target"
        return None


def load_facts(master_path: Path | str | None = None, variants_path: Path | str | None = None, vocab_path: Path | None = None) -> Facts:
    master_path = Path(master_path) if master_path else DEFAULT_MASTER
    if variants_path is None:
        sibling = master_path.with_name("variants.yaml")
        variants_path = sibling if sibling.exists() else None
    try:
        master = yaml.safe_load(master_path.read_text(encoding="utf-8"))
        variants = yaml.safe_load(Path(variants_path).read_text(encoding="utf-8")) if variants_path else None
    except (OSError, yaml.YAMLError) as exc:
        raise TraceabilityError(f"cannot read the fact file: {exc}") from exc
    if not isinstance(master, dict) or "experience" not in master:
        raise TraceabilityError(f"{master_path} is not a master CV fact file")
    return Facts(master, variants, load_vocab(vocab_path) if vocab_path else None)


# ----------------------------------------------------------------------------- documents and results

@dataclass
class Block:
    text: str
    facts: tuple[str, ...] = ()


@dataclass
class Document:
    blocks: list[Block]
    title: str | None = None            # the position line; the job applied for, not a claim about the user
    company: str | None = None          # target employer, allowed by name
    generated_on: date | None = None    # the date the letter is generated (made by code); that date and its year are allowed
    employer_location: str | None = None  # city (and address) of the target employer, for the letter header
    contact: str | None = None          # contact person named in the salutation
    posting: str | None = None          # title and text of the job, letters only: a term repeated from it is no claim (v0.78.9)

    def __post_init__(self):
        self.generated_on = parse_date(self.generated_on)


@dataclass(frozen=True)
class Finding:
    kind: str
    text: str
    block: int                           # index into Document.blocks, -1 for the title line
    reason: str
    block_text: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text, "block": self.block, "reason": self.reason, "block_text": self.block_text}


@dataclass
class TraceResult:
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    def by_kind(self) -> dict[str, list[Finding]]:
        out: dict[str, list[Finding]] = {}
        for f in self.findings:
            out.setdefault(f.kind, []).append(f)
        return out

    def to_dict(self) -> dict:
        return {"ok": self.ok, "findings": [f.to_dict() for f in self.findings]}

    def render_text(self) -> str:
        if self.ok:
            return "Traceability check passed: nothing in the document is missing from the fact file."
        lines = [f"Traceability check FAILED: {len(self.findings)} finding(s). The document is blocked."]
        for f in self.findings:
            where = "title line" if f.block < 0 else f"block {f.block + 1}"
            lines.append(f"- [{f.kind}] \"{f.text}\" ({where}): {f.reason}")
        return "\n".join(lines)


_CITE = re.compile(r"\[([A-Za-z][\w-]*)\]")


def document_from_json(obj) -> Document:
    if not isinstance(obj, dict) or not isinstance(obj.get("blocks"), list):
        raise TraceabilityError('a JSON document is an object with a "blocks" list (and optional "title" and "company")')
    blocks = []
    for i, b in enumerate(obj["blocks"], 1):
        if isinstance(b, str):
            blocks.append(Block(b))
        elif isinstance(b, dict) and isinstance(b.get("text"), str):
            ids = b.get("facts") or []
            if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
                raise TraceabilityError(f'block {i}: "facts" must be a list of fact ids')
            blocks.append(Block(b["text"], tuple(ids)))
        else:
            raise TraceabilityError(f'block {i} must be a string or an object with "text"')
    def text_field(key):
        v = obj.get(key)
        if v is not None and not isinstance(v, str):
            raise TraceabilityError(f'"{key}" must be a string')
        return v
    return Document(blocks, text_field("title"), text_field("company"), parse_date(text_field("generated_on")),
                    text_field("employer_location"), text_field("contact"))


def parse_date(value) -> date | None:
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise TraceabilityError(f'"{value}" is not a date; write it as YYYY-MM-DD') from exc


def document_from_text(text: str) -> Document:
    """Plain text: paragraphs are blocks, every bullet line is its own block, a first line 'Title: ...' is the title line."""
    title = None
    blocks: list[Block] = []
    current: list[str] = []

    def flush():
        if current:
            blocks.append(Block(" ".join(current)))
            current.clear()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
        elif re.match(r"title:\s*", stripped, re.I) and title is None and not blocks and not current:
            title = re.sub(r"^title:\s*", "", stripped, flags=re.I)
        elif re.match(r"[-*•]\s+", stripped):
            flush()
            current.append(re.sub(r"^[-*•]\s+", "", stripped))
        else:
            current.append(stripped)
    flush()
    return Document(blocks, title)


def load_document(path: Path | str) -> Document:
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise TraceabilityError(f"cannot read {p}: {exc}") from exc
    if p.suffix.lower() == ".json":
        try:
            return document_from_json(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise TraceabilityError(f"{p} is not valid JSON: {exc}") from exc
    return document_from_text(raw)


# ----------------------------------------------------------------------------- the check

# A clause in which the user says they LACK a tool is not a claim to have it ("I have not worked with Kafka",
# "Kafka is new to me", "not part of my background"). Checked per clause, so "not X, but I built Y" still flags Y.
_GAP_RE = re.compile(
    r"\b(?:have not|haven't|has not|hasn't|did not|didn't|do not|don't|never (?:used|worked|touched|had|done|built|managed|written|learned|learnt|heard|tried|seen|been|run|use|work|have)|no (?:[\w-]+ ){0,2}(?:experience|exposure|knowledge)|"
    r"new to me|not part of|not (?:yet )?(?:familiar|experienced)|lack\w*|not worked|unfamiliar)\b", re.IGNORECASE)
_CLAUSE_END_RE = re.compile(r"[.!?;\n]|,?\s+\b(?:but|however|although|while|whereas|though)\b|,\s+(?=(?:and|so|yet|I|we)\b)"
                            r"|\s+and\s+(?=(?:I|we|have|built|used|worked|delivered)\b)", re.IGNORECASE)


def _is_gap_statement(text: str, pos: int) -> bool:
    start = max([m.end() for m in _CLAUSE_END_RE.finditer(text, 0, pos)] or [0])
    m = _CLAUSE_END_RE.search(text, pos)
    return bool(_GAP_RE.search(text[start:m.start() if m else len(text)]))


# A cover letter may repeat a term of the posting without claiming it (v0.78.9). A skill or organisation is exempt only
# when the term is in the posting and the sentence attributes it to the job, looks ahead to the job, or contrasts with it.
# Titles, numbers, dates and certificates are never exempt, and neither is any text of a CV.
_FIRST_PERSON_RE = re.compile(r"\bI\b|\b[Mm]y\b|\b[Mm]e\b|\b[Mm]ine\b|\b[Ww]e\b|\b[Oo]ur\b")
_SENTENCE_END_RE = re.compile(r"[.!?;:](?=\s|$)|\n")
_PART_END_RE = re.compile(r"[.!?;:](?=\s|$)|\n|,\s+(?:and|but|while|so|though|although|whereas|yet)\b", re.IGNORECASE)
_ATTRIBUTED_RE = re.compile(
    r"\b(?:your|the)\s+(?:[\w/&-]+\s+){0,10}?(?:posting|profile|role|position|contract|vacancy|advert\w*|job)\b.*?"
    r"\b(?:asks?|requires?|needs?|wants?|seeks?|expects?|mentions?|names?|lists?|combines?|centres?|centers?|focuses|involves?|covers?)\b"
    r"|\b(?:is|are)\s+(?:recruiting|hiring|seeking|looking\s+for)\b", re.IGNORECASE | re.DOTALL)
_AHEAD_RE = re.compile(r"\b(?:interview|conversation|call|meeting)\b.*?\bhow I (?:can|could|would|might)\b", re.IGNORECASE | re.DOTALL)
_PAST_CLAIM_RE = re.compile(r"\b(?:experience|background|expertise|built|used|delivered|worked|drawing)\b", re.IGNORECASE)
_CONTRAST_RE = re.compile(r"\brather\s+than\s+$", re.IGNORECASE)


class _Checker:
    def __init__(self, facts: Facts, allowed_employers):
        self.f = facts
        v = facts.vocab
        self.v = v
        self.extra = [w for w in (facts._org_words(a) for a in allowed_employers if a) if w]
        cs = set(v.get("case_sensitive", []))
        self.skill_terms = [(t, _term_regex(t, t in cs)) for t in v.get("skills_extra", [])]
        self.cert_terms = [(t, _term_regex(t, t in cs)) for t in v.get("certificates", [])]
        self.suffixes = {s.lower() for s in v.get("org_suffixes", [])}
        self.known_orgs = []
        for name in v.get("known_organisations", []):
            self.known_orgs.append((name, _term_regex(name, " " not in name and "&" not in name)))
        self.roles = {r.lower() for r in v.get("role_nouns", [])}
        self.prefix_ignore = {x.lower() for x in v.get("title_prefix_ignore", [])}
        self.neutral = {fold(x) for x in v.get("neutral_terms", [])}
        self.locations = set(facts.locations)
        self.extra_values: set[Decimal] = set()
        self.generated_on: date | None = None
        self.target_title: tuple[str, ...] = ()            # words of the posting's title (set per document)
        self.posting = ""                                  # the words of the posting, letters only (set per document)
        self.prep = [w.lower() for w in v.get("preparation_words", [])]
        self.skill_words = {fold(t) for t, _ in self.skill_terms}
        self.literal_patterns = [re.compile(r"\+?" + r"\D*".join(re.escape(d) for d in re.sub(r"\D", "", x))) for x in facts.literals
                                 if len(re.sub(r"\D", "", x)) >= 7]
        self.literal_strings = [x for x in facts.literals if len(re.sub(r"\D", "", x)) < 7]

    # -- entry
    def check(self, doc: Document) -> TraceResult:
        res = TraceResult()
        seen: set[tuple] = set()

        def add(kind, text, idx, reason, block_text):
            key = (kind, text.strip(), idx)
            if key not in seen:
                seen.add(key)
                res.findings.append(Finding(kind, text.strip(), idx, reason, " ".join(block_text.split())[:140]))

        items: list[tuple[int, Block, bool]] = []
        if doc.title:
            items.append((-1, Block(doc.title), True))
        items += [(i, b, False) for i, b in enumerate(doc.blocks)]
        extra_save = list(self.extra)
        saved = (set(self.locations), set(self.neutral), set(self.extra_values), self.generated_on)
        self.generated_on = doc.generated_on
        self.target_title = words(doc.title) if doc.title and doc.company else ()   # the importer always names the company
        # a scraped posting can glue two words ("Mélanie MoosHuman Resources"): the split form is added (v0.85.6)
        unglued = re.sub(r"(?<=[a-zà-ÿ])(?=[A-ZÀ-Þ])", " ", doc.posting or "")
        self.posting = " " + " ".join(words(doc.posting or "")) + " | " + " ".join(words(unglued)) + " "
        if doc.employer_location:                          # "8001 Zuerich": its words are places, its numbers are allowed
            self.locations |= {fold(doc.employer_location)} | {fold(p) for p in re.split(r"[,\n]", doc.employer_location) if p.strip()}
            self.neutral |= set(words(doc.employer_location))
            self.extra_values |= {n.value for n in extract_figures(doc.employer_location)[0]}
        if doc.contact:                                    # "Dear Ms McDonald," is not an unknown term
            self.neutral |= set(words(doc.contact))
        if doc.company:
            w = self.f._org_words(doc.company)
            if w:
                self.extra.append(w)
        for idx, block, is_title in items:
            text = block.text
            cited = list(block.facts) + _CITE.findall(text)
            for fid in cited:
                if fid not in self.f.ids:
                    add("unknown_fact_id", fid, idx, "the cited fact id does not exist in the fact file", block.text)
            text = _CITE.sub(" ", text)
            text = re.sub(r"^\s*(?:\d+[.)]|[-*•])\s+", "", text)
            text = self._mask(text)
            self._numbers(text, idx, add, block.text)
            self._orgs_and_titles(text, idx, is_title, add, block.text)
            self._skills(text, idx, add, block.text)
            self._certificates(text, idx, cited, add, block.text)
        self.extra = extra_save
        self.locations, self.neutral, self.extra_values, self.generated_on = saved
        self.target_title = ()
        self.posting = ""
        return res

    def _repeats_posting(self, text: str, start: int, end: int, term: str) -> bool:
        """The term comes from the posting and this sentence talks about the job, so it is no claim about the user."""
        if " " + " ".join(words(term)) + " " not in self.posting:
            return False
        if _CONTRAST_RE.search(text[:start]):
            return True                                    # "in banking rather than AdTech"
        begin = max([m.end() for m in _SENTENCE_END_RE.finditer(text, 0, start)] or [0])
        stop = _PART_END_RE.search(text, end)
        part = text[begin:stop.start() if stop else len(text)]
        ahead = _AHEAD_RE.search(text[begin:start])
        if ahead:
            rest = part[ahead.end():]
            return not (_FIRST_PERSON_RE.search(rest) or _PAST_CLAIM_RE.search(rest))
        return not _FIRST_PERSON_RE.search(part) and bool(_ATTRIBUTED_RE.search(part))

    def _mask(self, text: str) -> str:
        text = _clean(text)
        text = re.sub(r"https?://\S+|www\.\S+|\b[\w.+-]+@[\w-]+\.[\w.-]+\b", " ", text)
        for p in self.literal_patterns:
            text = p.sub(" ", text)
        return text

    # -- numbers and dates
    def _numbers(self, text, idx, add, block_text):
        nums, dates, _codes = extract_figures(text)
        f = self.f
        for d in dates:
            g = self.generated_on
            if g and d.year == g.year and d.month in (None, g.month):
                continue                                   # the date the letter is generated (by code, not by the session)
            if d.month is not None:
                if (d.year, d.month) not in f.year_months:
                    add("date", d.text, idx, "this month and year is not a start or end date in the fact file", block_text)
            elif d.year not in f.years:
                add("date", d.text, idx, "this year is not a start or end year in the fact file", block_text)
        for n in nums:
            if n.kind == "pct":
                ok = n.value in f.pct
                why = "this percentage does not appear in the fact file"
            elif n.kind == "money":
                ok = (n.value, n.cur) in f.money
                why = f"this amount in {n.cur} does not appear in the fact file"
            else:
                ok = n.value in f.values or n.value in self.extra_values
                why = "this number does not appear in the fact file"
            if not ok:
                add("number", n.text, idx, why, block_text)

    # -- employers and titles
    def _split_parts(self, cand: str) -> list[str]:
        return [p.strip() for p in re.split(r"\s+(?:and|&)\s+|\s*&\s*", cand) if p.strip()]

    def _org_status(self, cand: str, sentence_start: bool = False, corpus_ok: bool = False) -> str | None:
        """None when allowed (or nothing to judge), else the part that is unknown."""
        if self.f.match_org(cand, self.extra) is not None:
            return None                                    # the whole name: "Bosshard & Partner AG" is one company (v0.78.9)
        for part in self._split_parts(cand):
            m = self.f.match_org(part, self.extra)
            if m is None and sentence_start and " " in part:
                m = self.f.match_org(part.split(" ", 1)[1], self.extra)
            if m is None and corpus_ok:
                rest = part.split(" ", 1)[1] if sentence_start and " " in part else part
                if fold(part) in self.f.corpus or fold(rest) in self.f.corpus:
                    m = "corpus"
            if m is None and self._in_target_title(part, sentence_start):
                m = "target_title"                         # the posting's own job title, copied into the letter
            if m is None:
                return part
        return None

    def _in_target_title(self, part: str, sentence_start: bool = False) -> bool:
        t = self.target_title
        if not t:
            return False
        def inside(w):
            return bool(w) and any(t[i:i + len(w)] == w for i in range(len(t) - len(w) + 1))
        w = words(part)
        return inside(w) or (sentence_start and inside(w[1:]))

    def _org_kind(self, cand: str) -> str | None:
        parts = self._split_parts(cand)
        for part in parts:
            m = self.f.match_org(part, self.extra)
            if m and m.startswith("fact:"):
                return m
        return None

    def _orgs_and_titles(self, text, idx, is_title, add, block_text):
        f = self.f
        toks = tokenize(text)
        low = [t.text.lower() for t in toks]
        runs: list[tuple[str, int, int]] = []              # (candidate text, token start, token end)

        def run_text(i, j):
            return text[toks[i].start:toks[j - 1].end]

        cand_seen: set[str] = set()

        def flag(cand: str, why: str, pos: int, sentence_start=False, corpus_ok=False):
            if fold(cand) in cand_seen or self._repeats_posting(text, pos, pos + len(cand), cand):
                return
            bad = self._org_status(cand, sentence_start, corpus_ok)
            if bad is None:
                return
            cand_seen.add(fold(cand))
            add("employer", cand, idx, why, block_text)

        # (c) organisation named after an employment cue
        org_after: dict[int, tuple[int, int]] = {}
        for i, t in enumerate(low):
            start = None
            if t in ("at", "@", "joined") and i + 1 < len(toks):
                start = i + 1
            elif t in ("by", "at", "for") and i >= 1 and low[i - 1] in ("employed", "worked", "working") and i + 1 < len(toks):
                start = i + 1
            if start is not None and toks[start].cap and _contiguous(text, toks[start - 1], toks[start]):
                j = cap_run(text, toks, start)
                cand = run_text(start, j)
                org_after[i] = (start, j)
                if fold(cand) in self.locations or any(fold(p) in self.locations for p in self._split_parts(cand)):
                    continue
                if fold(cand) in self.skill_words:
                    continue
                flag(cand, "this employer is not in the fact file and is not the employer being applied to", toks[start].start)
        # (b) capitalised phrases that end in a company-type word
        i = 0
        while i < len(toks):
            if toks[i].cap:
                j = cap_run(text, toks, i)
                if j - i >= 2 and any(toks[k].text.lower() in self.suffixes for k in range(i, j)):
                    cand = run_text(i, j)
                    fc = fold(cand)
                    if (fc not in f.corpus and fc not in self.locations and not re.search(r"certif", fc)
                            and not any(seen_c in fc for seen_c in cand_seen)):
                        sentence_start = i == 0 or bool(re.search(r"[.!?:;\n]\s*$", text[:toks[i].start]))
                        flag(cand, "this looks like an organisation that is not in the fact file and not the employer being applied to", toks[i].start, sentence_start, True)
                i = j
            else:
                i += 1
        # (a) organisations that are recognised by name
        for name, rx in self.known_orgs:
            for m in rx.finditer(text):
                flag(m.group(), "this organisation is not in the fact file and is not the employer being applied to", m.start())
        # titles
        for t_idx, claim, org in self._title_claims(text, toks, low, org_after, is_title):
            self._check_title(claim, org, idx, add, block_text)

    def _title_claims(self, text, toks, low, org_after, is_title):
        claims = []
        n = len(toks)
        # "as a <Title> [at <Org>]"
        for i, t in enumerate(low):
            if t != "as":
                continue
            k = i + 1
            if k < n and low[k] in ("a", "an", "the"):
                k += 1
            last, end = None, k
            while end < n and end - k < 6 and (end == k or _contiguous(text, toks[end - 1], toks[end])):
                if low[end] in self.roles:
                    last = end
                end += 1
            if last is None or is_title:
                continue
            title_toks = [toks[x].text for x in range(k, last + 1)]
            if title_toks[0].lower() in ("your", "my", "our", "their", "his", "her", "its", "this", "that"):
                continue                                   # "as your analyst" is not a title of the user
            while title_toks and title_toks[0].lower() in self.prefix_ignore:
                title_toks.pop(0)
            org = None
            nxt = last + 1
            if nxt + 1 < n and low[nxt] in ("at", "for", "with", "in") and toks[nxt + 1].cap and _contiguous(text, toks[last], toks[nxt]):
                j = cap_run(text, toks, nxt + 1)
                org = text[toks[nxt + 1].start:toks[j - 1].end]
            claims.append((i, " ".join(title_toks), org))
        # "<Title> at <Org>"
        for i, t in enumerate(low):
            if t not in ("at", "@") or i == 0 or i + 1 >= n or not toks[i + 1].cap or low[i - 1] not in self.roles:
                continue
            if is_title:
                continue
            k = i - 1
            while k - 1 >= 0 and (toks[k - 1].cap or toks[k - 1].text == "/") and _contiguous(text, toks[k - 1], toks[k]) and i - 1 - k < 5:
                k -= 1
            title_toks = [toks[x].text for x in range(k, i)]
            while title_toks and title_toks[0].lower() in self.prefix_ignore:
                title_toks.pop(0)
            j = cap_run(text, toks, i + 1)
            claims.append((i, " ".join(title_toks), text[toks[i + 1].start:toks[j - 1].end]))
        return claims

    def _check_title(self, claim: str, org: str | None, idx, add, block_text):
        f = self.f
        w = words(claim)
        if not w:
            return
        w = tuple({"sr": "senior", "jr": "junior"}.get(x, x) for x in w)
        key = None
        if org:
            kind = self._org_kind(org)
            if kind:
                key = kind[5:]
            elif self.f.match_org(org, self.extra) == "target":
                return                                 # the job being applied to, not a claim about the past
        pool = f.titles_by_key.get(key, set()) if key else f.all_titles

        def contiguous_in(small, big):
            return any(big[i:i + len(small)] == small for i in range(len(big) - len(small) + 1))
        if any(contiguous_in(w, t) for t in pool):
            return
        where = f" at {org}" if org and key else ""
        add("title", claim, idx, f"this is not a job title in the fact file{where}", block_text)

    # -- skills
    def _skills(self, text, idx, add, block_text):
        f = self.f
        clean = re.sub(_CODE.pattern, " ", text)
        for term, rx in self.skill_terms:
            if rx.search(f.corpus_raw):
                continue
            for m in rx.finditer(clean):                           # every mention: one repeated from the posting does not excuse a claim
                if not _is_gap_statement(clean, m.start()) and not self._repeats_posting(clean, m.start(), m.end(), m.group()):
                    add("skill", m.group(), idx, "this skill or tool is not in the fact file", block_text)
                    break
        allowed_orgs = {w for ew, _ in f.org_entries for w in ew} | {w for e in self.extra for w in e}
        upper_ratio = sum(c.isupper() for c in clean) / max(1, sum(c.isalpha() for c in clean))
        for m in re.finditer(r"(?<![\w-])(?:[A-Z]{2,8}s?|[A-Z][a-z]+[A-Z][A-Za-z]*|[a-z]+[A-Z][A-Za-z]*)(?![\w-])", clean):
            tok = m.group()
            if upper_ratio > 0.6 and tok.isupper():
                continue                                           # a heading in capitals
            low = fold(tok)
            if _is_gap_statement(clean, m.start()) or self._repeats_posting(clean, m.start(), m.end(), tok):
                continue                                           # "Kafka is new to me": a gap, not a claim; or the posting's own term
            if self._in_title_phrase(clean, m.start(), m.end(), low):
                continue                                           # a term of the posting's own title, named with its neighbours ("Genesys Cloud CX")
            if low in f.corpus_words or low.rstrip("s") in f.corpus_words or low + "s" in f.corpus_words:
                continue
            if low in self.neutral or low in allowed_orgs or low in self.skill_words:
                continue
            if any(rx.search(tok) for _, rx in self.skill_terms + self.cert_terms):
                continue                                           # reported (or allowed) by the vocabulary rule
            if any(low == fold(n) for n, _ in self.known_orgs):
                continue                                           # reported by the employer rule
            add("skill", tok, idx, "this term (acronym or product name) is not in the fact file", block_text)

    def _in_title_phrase(self, clean: str, start: int, end: int, low: str) -> bool:
        """The token stands next to a neighbouring word of the posting's title, in the title's order ("Cloud CX").
        The bare term alone ("I know IAM") is not exempt: that is a claim and needs a fact."""
        t = self.target_title
        before = words(clean[:start])[-1:]
        after = words(clean[end:])[:1]
        for i, w in enumerate(t):
            if w != low:
                continue
            if before and i > 0 and t[i - 1] == before[0]:
                return True
            if after and i + 1 < len(t) and t[i + 1] == after[0]:
                return True
        return False

    # -- certificates
    def _certificates(self, text, idx, cited, add, block_text):
        f = self.f
        folded = fold(text)
        for code in extract_figures(text)[2]:
            if fold(code) not in f.corpus:
                add("certificate", code, idx, "this exam or certificate code is not in the fact file", block_text)
        for term, rx in self.cert_terms:
            m = rx.search(text)
            if m and not rx.search(f.corpus_raw):
                add("certificate", m.group(), idx, "this certificate is not in the fact file", block_text)
        toks = tokenize(text)
        for i, t in enumerate(toks):
            if t.text.lower() not in ("certified", "certification", "certifications", "certificate", "certificates"):
                continue
            left = i
            while left - 1 >= 0 and i - left < 4 and _contiguous(text, toks[left - 1], toks[left]) and (toks[left - 1].cap or re.search(r"\d", toks[left - 1].text)):
                left -= 1
            right = i + 1
            while right < len(toks) and right - i <= 3 and _contiguous(text, toks[right - 1], toks[right]) and toks[right].cap:
                right += 1
            if left == i and right == i + 1:
                if not any(c in f.cert_fact_ids for c in cited):
                    add("certificate", t.text, idx, "a certificate is claimed without a name, and no cited fact mentions one", block_text)
                continue
            phrase = fold(text[toks[left].start:toks[right - 1].end])
            if phrase not in f.corpus:
                add("certificate", text[toks[left].start:toks[right - 1].end], idx, "this certificate is not in the fact file", block_text)
        for cert in f.certs:
            if cert["status"] in ("in_preparation", "in_progress", "not_obtained"):
                core = fold(re.sub(r"\(.*?\)", "", cert["name"])).strip()
                code = (re.search(r"\(([^)]+)\)", cert["name"]) or [None, ""])[1]
                mentioned = (core and core in folded) or (code and fold(code) in folded)
                if not mentioned and re.search(r"oracle\b.{0,40}\bcertif|certified\b.{0,30}\boracle|oracle\b.{0,60}\bassociate", folded):
                    mentioned = True
                if mentioned and not any(p in folded for p in self.prep):
                    add("certificate", cert["name"], idx,
                        "the fact file lists this certificate as not obtained; the text must say you are preparing for it", block_text)


def check_document(doc: Document, facts: Facts, *, allowed_employers=(), generated_on=None, employer_location=None,
                   contact=None) -> TraceResult:
    """Run every check. `allowed_employers`: names that may appear besides the fact-file employers, above all the
    employer being applied to (a cover letter names the company it is addressed to). `generated_on` (a date or
    YYYY-MM-DD), `employer_location` and `contact` come from the caller (they are also fields of `Document`): the
    letter's date, the employer's city and the name in the salutation are allowed without being facts about the user.
    Any other year or date that is not in the fact file is still a finding."""
    if generated_on is not None or employer_location or contact:
        doc = Document(doc.blocks, doc.title, doc.company, parse_date(generated_on) or doc.generated_on,
                       employer_location or doc.employer_location, contact or doc.contact, doc.posting)
    return _Checker(facts, list(allowed_employers)).check(doc)
