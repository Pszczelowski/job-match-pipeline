"""When is a description complete (goal.md 5.1a)?

At least 1,500 characters (500 for text the user pastes, v0.85.2), with a tasks part and a requirements part. A heuristic over German, English, French and Polish
headings: a text that is long but has no recognisable "what you will do" and "what we expect" part is stored as partial
and does not satisfy `tailoring.require_description`.
"""
from __future__ import annotations

import re

MIN_CHARS = 1500
MANUAL_MIN_CHARS = 500          # manual paths (web paste, set-description, import-descriptions): some postings are that short (v0.85.2)

TASK_MARKERS = (
    # German
    "aufgaben", "verantwortung", "das erwartet", "deine rolle", "ihre rolle", "ihr einsatz", "dein einsatz", "tätigkeit",
    "gestaltungsraum", "inhalt der rolle", "ihr wirkungsfeld", "dein wirkungsfeld",
    # English
    "responsibilit", "tasks", "what you will do", "what you'll do", "what you’ll do", "your role", "your impact", "duties",
    "key accountabilities", "the role", "your challenge", "how you'll make", "how you’ll make", "position description",
    "mission/purpose", "your mission",
    # French
    "missions", "tâches", "votre rôle",
    # Polish
    "zadania", "obowiązki",
)
# Only as a heading on its own line: "you will" inside a sentence is in nearly every text and proves nothing (v0.78.4).
TASK_HEADING = re.compile(r"^\s*you(?: will|['’]ll)\s*:?\s*$", re.IGNORECASE | re.MULTILINE)
REQUIREMENT_MARKERS = (
    # German
    "profil", "anforderung", "das bringst", "bringen sie mit", "bringst du mit", "du mitbringst", "sie mitbringen", "qualifikation", "voraussetzung", "erfahrung", "kenntnisse",
    # English
    "requirements", "qualifications", "your profile", "skills", "you bring", "what we expect", "experience", "must have", "your background", "candidate profile",
    # French
    "exigences", "compétences", "expérience", "vous avez", "votre profil",
    # Polish
    "wymagania", "oczekujemy", "doświadczeni",
)


def has_markers(text: str) -> tuple[bool, bool]:
    low = (text or "").casefold()
    return any(m in low for m in TASK_MARKERS) or bool(TASK_HEADING.search(text or "")), any(m in low for m in REQUIREMENT_MARKERS)


def is_complete(text: str) -> bool:
    text = (text or "").strip()
    if len(text) < MIN_CHARS:
        return False
    tasks, requirements = has_markers(text)
    return tasks and requirements


def manual_problems(text: str) -> list[str]:
    """Why the manual paths refuse a text: only when it is shorter than 500 characters (v0.85.3). A missing tasks or
    requirements part is a warning there (`missing_parts`), the text is stored: the user decided it is the posting."""
    text = (text or "").strip()
    return [f"only {len(text)} characters (at least {MANUAL_MIN_CHARS:,})"] if len(text) < MANUAL_MIN_CHARS else []


def missing_parts(text: str) -> list[str]:
    """The parts the heuristic did not find, in words for the user (empty list: both found)."""
    tasks, requirements = has_markers((text or "").strip())
    found = []
    if not tasks:
        found.append("no tasks part found (headings such as Aufgaben, Responsibilities, Missions)")
    if not requirements:
        found.append("no requirements part found (headings such as Profil, Requirements, Exigences)")
    return found


def problems(text: str) -> list[str]:
    """Why a text is not complete, in words for the user (empty list: it is complete). Used by the manual paths, which
    refuse an incomplete text (goal.md 5.1, v0.72.0). The same rules as `is_complete`, with the lower manual minimum
    of 500 characters (v0.85.2)."""
    text = (text or "").strip()
    found: list[str] = []
    if len(text) < MANUAL_MIN_CHARS:
        found.append(f"only {len(text)} characters (at least {MANUAL_MIN_CHARS:,})")
    tasks, requirements = has_markers(text)
    if not tasks:
        found.append("no tasks part found (headings such as Aufgaben, Responsibilities, Missions)")
    if not requirements:
        found.append("no requirements part found (headings such as Profil, Requirements, Exigences)")
    return found
