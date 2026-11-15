"""The versioned scoring prompt (goal.md 5.4).

The prompt is a Markdown file in `prompts/`. Its version is the first line that reads `Prompt version: <x>`.
That version goes into every batch file and every stored score. A version that ends in `-draft` is not
approved for real scoring: `load_prompt(..., allow_draft=False)` refuses it.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

from .config import resolve_path

PART_TIME_DEDUCTION = 5
VERSION_RE = re.compile(r"^Prompt version:\s*(\S+)\s*$", re.MULTILINE)


class PromptError(ValueError):
    """The prompt file is missing, empty, unversioned or not approved."""


def load_prompt(cfg: dict, allow_draft: bool = False, path: str | Path | None = None) -> tuple[str, str]:
    """Return (text, version) of the scoring prompt named in the config."""
    file = Path(path) if path else resolve_path(cfg["scoring"]["prompt_file"])
    if not file.exists():
        raise PromptError(f"prompt file not found: {file}")
    text = file.read_text(encoding="utf-8")
    if not text.strip():
        raise PromptError(f"prompt file is empty: {file}")
    match = VERSION_RE.search(text)
    if not match:
        raise PromptError(f"{file.name} has no 'Prompt version: <x>' line")
    version = match.group(1)
    if version.endswith("-draft") and not allow_draft:
        raise PromptError(f"prompt version {version} is a draft and not approved for real scoring")
    return text, version


def weighted_overall(subscores: dict, weights: dict, cap: int | None = None, part_time: bool = False) -> int:
    """The overall score of the rubric: weighted average of the sub-scores, rounded half up, at most `cap` if given;
    a part-time posting then takes 5 points off after the cap, never below 0 (prompt 1.12.0)."""
    value = math.floor(sum(weights[k] * subscores[k] for k in weights) / 100 + 0.5)
    value = min(value, cap) if cap is not None else value
    return max(0, value - PART_TIME_DEDUCTION) if part_time else value
