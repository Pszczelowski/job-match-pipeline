"""Language-requirement filter (goal.md 5.3.2). Rules only, no LLM.

Polish and English never matter. For every other language the offer mentions as a requirement:
  C1/C2 or a "fluent"-type phrase  -> fail         language_c1_required
  a level below C1 (A1-B2)         -> conditional  language_required_below_c1
  required, no level stated        -> conditional  language_level_unspecified
  only a plus / nice to have       -> pass         language_optional   (kept as a note)
  German posting, no requirement   -> conditional  language_implied_by_posting
  no description and no mention    -> pass         language_not_assessed
The worst outcome over all languages wins (done by the runner); every language gets its own result.

How a mention is read. The text (title + description) is cut into segments (lines, bullets, sentences).
In a segment, language names (watched and ignored ones) are found; then three kinds of tokens are
attached to them: CEFR levels (A1..C2, ranges read as their minimum), "fluent"-type phrases, and
"plus"-type phrases.
  * a token before a mention applies to that mention and to the mention chain joined to it by
    "and / und / , / &" ("fluent in English and German" is fluent German); a level before a mention
    applies to that mention only;
  * a token after a mention applies to that mention ("German C1 and French B2"); a "plus" phrase also
    applies to the whole chain before it ("French and German are a plus");
  * a level after a chain that is marked as shared ("jeweils / each / both / beide" before it, or the level in
    parentheses right after the group) applies to every mention of the chain that has no level of its own
    ("sehr gute Deutsch- und Englischkenntnisse (jeweils C1 Level)" is C1 German);
  * tokens farther than MAX_GAP characters from a mention are not attached.
A watched language that is mentioned with no level, no phrase and no requirement cue nearby (for
example "we offer German lessons") is not a requirement and is ignored.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .base import FilterResult, JobContext

MAX_GAP = 40            # characters between a token and the mention it belongs to
CUE_WINDOW = 60         # characters around a mention searched for a requirement cue
SNIPPET = 160
LEVELS = {"A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6}
LEVEL_RE = re.compile(r"\b([ABC][12])\b")                      # upper case only: "a1" is not a level
RANGE_RE = re.compile(r"\b([ABC][12])\s*(?:-|–|—|/|to|bis|à|or)\s*([ABC][12])\b")
SEGMENT_RE = re.compile(r"[\r\n•▪●◦;]+|(?<=[.!?])\s+")
CHAIN_GAP_RE = re.compile(r"^(?:\s|,|/|&|-|\bsowie\b|\bas well as\b|\band\b|\bund\b|\bor\b|\boder\b|\bet\b)*$", re.IGNORECASE)

# A level that is shared by a joined group: "Deutsch und Englisch (jeweils C1)", "German and English, both at C1".
DISTRIBUTIVE_RE = re.compile(r"\b(?:jeweils|each|both|beide|beides)\b", re.IGNORECASE)
PAREN_ONLY_RE = re.compile(r"^\s*\(\s*$")                       # the level sits in parentheses right after the group

# Rank of a reading, used to pick the strongest one per language.
RANK = {"language_c1_required": 4, "language_required_below_c1": 3, "language_level_unspecified": 2, "language_optional": 1}
OUTCOME = {"language_c1_required": "fail", "language_required_below_c1": "conditional",
           "language_level_unspecified": "conditional", "language_optional": "pass"}

GERMAN_WORDS = set("""und der die das wir sie mit für ein eine ist zu von auf im den nicht werden bei als oder auch sind
dem des uns ihre ihr haben einen aus nach über durch sowie unser unsere""".split())
ENGLISH_WORDS = set("""the and of to in for with you we is are will as our your be on that this have or an at from by
their can""".split())


@dataclass
class _Mention:
    start: int
    end: int
    language: str | None        # None: an ignored language (English, Polish)


@dataclass
class _Token:
    kind: str                   # "level", "c1", "optional"
    start: int
    end: int
    value: int = 0              # level value for kind == "level"
    label: str = ""


@dataclass
class _Reading:
    language: str
    code: str
    quote: str
    detail: str = ""


def _alt(patterns: list[str]) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(patterns) + r")\b", re.IGNORECASE)


def _segments(text: str) -> list[str]:
    return [s.strip() for s in SEGMENT_RE.split(text or "") if s and s.strip()]


def _mentions(segment: str, watched: list[tuple[str, re.Pattern]], ignored: re.Pattern) -> list[_Mention]:
    found = [_Mention(m.start(), m.end(), None) for m in ignored.finditer(segment)]
    for name, pattern in watched:
        found += [_Mention(m.start(), m.end(), name) for m in pattern.finditer(segment)]
    found.sort(key=lambda m: m.start)
    # drop a mention that overlaps an earlier one (e.g. two patterns matching the same word)
    kept: list[_Mention] = []
    for m in found:
        if not kept or m.start >= kept[-1].end:
            kept.append(m)
    return kept


def _tokens(segment: str, c1: re.Pattern, optional: re.Pattern) -> list[_Token]:
    tokens: list[_Token] = []
    covered: list[tuple[int, int]] = []
    for m in RANGE_RE.finditer(segment):
        low = min(LEVELS[m.group(1)], LEVELS[m.group(2)])
        tokens.append(_Token("level", m.start(), m.end(), low, m.group(0)))
        covered.append((m.start(), m.end()))
    for m in LEVEL_RE.finditer(segment):
        if not any(a <= m.start() < b for a, b in covered):
            tokens.append(_Token("level", m.start(), m.end(), LEVELS[m.group(1)], m.group(1)))
    tokens += [_Token("c1", m.start(), m.end(), label=m.group(0)) for m in c1.finditer(segment)]
    tokens += [_Token("optional", m.start(), m.end(), label=m.group(0)) for m in optional.finditer(segment)]
    return tokens


def _chains(segment: str, mentions: list[_Mention]) -> list[list[int]]:
    """Indexes of mentions joined by 'and / und / , / &' form one chain."""
    chains: list[list[int]] = []
    for i, m in enumerate(mentions):
        if chains and CHAIN_GAP_RE.match(segment[mentions[i - 1].end:m.start]):
            chains[-1].append(i)
        else:
            chains.append([i])
    return chains


def _attach(segment: str, mentions: list[_Mention], tokens: list[_Token]) -> dict[int, list[_Token]]:
    """Map mention index -> the tokens that belong to it."""
    attached: dict[int, list[_Token]] = {i: [] for i in range(len(mentions))}
    chains = _chains(segment, mentions)
    chain_of = {i: c for c in chains for i in c}
    shared: list[tuple[_Token, int]] = []
    for t in tokens:
        before = [i for i, m in enumerate(mentions) if m.start >= t.end and m.start - t.end <= MAX_GAP]
        after = [i for i, m in enumerate(mentions) if m.end <= t.start and t.start - m.end <= MAX_GAP]
        nxt = min(before, key=lambda i: mentions[i].start) if before else None
        prev = max(after, key=lambda i: mentions[i].end) if after else None
        # choose the nearer side; a tie goes to the mention before the token
        d_next = mentions[nxt].start - t.end if nxt is not None else None
        d_prev = t.start - mentions[prev].end if prev is not None else None
        use_prev = prev is not None and (d_next is None or d_prev <= d_next)
        if use_prev:
            targets = [prev]
            if t.kind == "optional":
                targets = [i for i in chain_of[prev] if i <= prev]
        elif nxt is not None:
            targets = [nxt] if t.kind == "level" else [i for i in chain_of[nxt] if i >= nxt]
        else:
            continue
        for i in targets:
            attached[i].append(t)
        if use_prev and t.kind == "level":
            gap = segment[mentions[prev].end:t.start]
            if DISTRIBUTIVE_RE.search(gap) or PAREN_ONLY_RE.match(gap):
                # a shared level: it also covers the earlier mentions of the chain that have no level of their own
                shared.append((t, prev))
    for t, prev in shared:
        for i in chain_of[prev]:
            if i < prev and not any(x.kind == "level" for x in attached[i]):
                attached[i].append(t)
    return attached


def _read(language: str, segment: str, mention: _Mention, toks: list[_Token], cues: re.Pattern) -> _Reading | None:
    quote = segment if len(segment) <= SNIPPET else segment[:SNIPPET].rstrip() + "..."
    levels = [t for t in toks if t.kind == "level"]
    if any(t.kind == "optional" for t in toks):
        return _Reading(language, "language_optional", quote, "only a plus")
    c1 = [t for t in toks if t.kind == "c1"]
    top = max(levels, key=lambda t: t.value, default=None)
    if c1 or (top and top.value >= LEVELS["C1"]):
        trigger = c1[0].label if c1 else top.label
        return _Reading(language, "language_c1_required", quote, f"'{trigger}' read as C1 or above")
    if top:
        return _Reading(language, "language_required_below_c1", quote, f"level {top.label}")
    lo, hi = max(0, mention.start - CUE_WINDOW), min(len(segment), mention.end + CUE_WINDOW)
    if cues.search(segment[lo:hi]):
        return _Reading(language, "language_level_unspecified", quote, "required, no level stated")
    return None


def _looks_german(text: str, cfg_gp: dict) -> bool:
    words = re.findall(r"[^\W\d_]+", text.casefold())
    german = sum(w in GERMAN_WORDS for w in words)
    english = sum(w in ENGLISH_WORDS for w in words)
    return german >= cfg_gp["min_german_words"] and german / max(english, 1) >= cfg_gp["min_ratio"]


def posting_is_german(description: str | None, cfg: dict) -> bool:
    """True when the description is written in German (same detector and thresholds as the language filter).

    Used for the non-English cap of the score (goal.md 5.4): only German is detected in code."""
    return bool(description and description.strip()) and _looks_german(description, cfg["languages"]["german_posting"])


SINGULAR_NEXT = re.compile(r"\s+(?:is|was)\b", re.IGNORECASE)
JOIN_NO_COMMA = re.compile(r"^\s*(?:/|&|\band\b|\bund\b|\bor\b|\boder\b|\bet\b)\s*$", re.IGNORECASE)
NEXT_WORD = re.compile(r"\s+(?:{nouns})\b(?:\(s\))?", re.IGNORECASE)


def _split_clauses(segment: str, mentions: list[_Mention]) -> list[str]:
    """Cut a segment at ", <language> is ..." so that "fluent in Spanish and English, German is a plus" is two clauses.

    Without the cut the comma would join German to the chain of the first clause and the "plus" would cover Spanish.
    The same for a short list before the verb: in "fluent German and English, French or Italian is an advantage" the
    advantage belongs to "French or Italian" only (31 Oct 2026: it had covered German and let a C1 requirement through).
    """
    cuts = []
    for i, m in enumerate(mentions):
        if not (m.language and SINGULAR_NEXT.match(segment, m.end)):
            continue
        j = i                                                      # walk left along "A or B is": the list the verb belongs to
        while j > 0 and JOIN_NO_COMMA.match(segment[mentions[j - 1].end:mentions[j].start]):
            j -= 1
        if segment[:mentions[j].start].rstrip().endswith(","):
            cuts.append(mentions[j].start)
    if not cuts:
        return [segment]
    cuts = sorted({segment[:c].rstrip().rfind(",") for c in cuts})
    parts, last = [], 0
    for c in cuts:
        parts.append(segment[last:c])
        last = c + 1
    parts.append(segment[last:])
    return [p.strip() for p in parts if p.strip()]


def _drop_descriptive(segment: str, mentions: list[_Mention], nouns: str) -> list[_Mention]:
    """A watched language followed by "market", "clients", "team" ... names a place or people, not a skill.

    The whole list counts: in "the German and Italian markets" both words describe the markets.
    """
    pattern = re.compile(NEXT_WORD.pattern.replace("{nouns}", nouns), re.IGNORECASE)
    dropped = [m.language is not None and bool(pattern.match(segment, m.end)) for m in mentions]
    for i in range(len(mentions) - 2, -1, -1):                       # carry the decision to the left along "A, B and C"
        if dropped[i + 1] and mentions[i].language is not None and CHAIN_GAP_RE.match(segment[mentions[i].end:mentions[i + 1].start]):
            dropped[i] = True
    return [m for m, gone in zip(mentions, dropped) if not gone]


def assess(text: str, cfg: dict) -> list[_Reading]:
    """All language readings found in the text, strongest one per language."""
    lang = cfg["languages"]
    watched = [(e["language"], _alt(e["patterns"])) for e in lang["names"]]
    ignored = _alt(lang["ignored_languages"])
    cues, c1, optional = _alt(lang["requirement_cues"]), _alt(lang["c1_phrases"]), _alt(lang["optional_phrases"])
    nouns = "|".join(lang["descriptive_nouns"])
    best: dict[str, _Reading] = {}
    clauses = [c for s in _segments(text) for c in _split_clauses(s, _mentions(s, watched, ignored))]
    for segment in clauses:
        mentions = _drop_descriptive(segment, _mentions(segment, watched, ignored), nouns)
        if not any(m.language for m in mentions):
            continue
        attached = _attach(segment, mentions, _tokens(segment, c1, optional))
        for i, m in enumerate(mentions):
            if m.language is None:
                continue
            reading = _read(m.language, segment, m, attached[i], cues)
            if reading and (m.language not in best or RANK[reading.code] > RANK[best[m.language].code]):
                best[m.language] = reading
    return list(best.values())


def language(job: JobContext, cfg: dict) -> list[FilterResult]:
    text = f"{job.title}\n{job.description}"
    readings = assess(text, cfg)
    results = [FilterResult("language", OUTCOME[r.code], r.code, f"{r.language}: {r.detail}; \"{r.quote}\"")
               for r in readings]
    requirement = any(r.code != "language_optional" for r in readings)
    if not requirement and not readings and job.description.strip():
        if _looks_german(job.description, cfg["languages"]["german_posting"]):
            return [FilterResult("language", "conditional", "language_implied_by_posting",
                                 "the posting is written in German and states no language requirement")]
    if not results:
        if not job.description.strip():
            return [FilterResult("language", "pass", "language_not_assessed",
                                 "no job description available; language requirements not checked")]
        return [FilterResult("language", "pass")]
    return results
