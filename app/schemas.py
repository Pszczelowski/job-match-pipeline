"""JSON schemas of the batch and results files (goal.md 5.10).

The schema version is "MAJOR.MINOR". The importer accepts only versions it knows (SUPPORTED_VERSIONS); a change
that old readers cannot handle gets a new major. The results schema is embedded in every batch file, so the
session that processes the batch sees exactly what it must write.

Version 1.1 (MINOR, additive) lets a score record carry an optional `documents` object: the text of a tailored
cover letter and CV for a job in the top places (goal.md 5.6, 5.10). The session writes only selection, order and
wording; employers, titles, dates, the letter header and the salutation come from code. A 1.0 file is still read.
"""
from __future__ import annotations

from jsonschema import Draft202012Validator

SCHEMA_VERSION = "1.1"
SUPPORTED_VERSIONS = ("1.0", "1.1")
SUBSCORES = ("skills", "experience", "domain", "seniority", "location")

_SCORE = {"type": "integer", "minimum": 0, "maximum": 100}
_TEXTS = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 300}, "maxItems": 20}

_ID = {"type": "string", "minLength": 1, "maxLength": 100}
_FACT_IDS = {"type": "array", "items": _ID, "minItems": 1, "maxItems": 6, "uniqueItems": True}

COVER_LETTER = {
    "type": "object",
    "additionalProperties": False,
    "required": ["contact_name", "blocks"],
    "properties": {
        # the person named in the posting, exactly as written there ("Ms Muster", "Anna Muster"); null when none is named
        "contact_name": {"type": ["string", "null"], "minLength": 1, "maxLength": 80},
        "blocks": {
            "type": "object",
            "additionalProperties": False,
            "required": ["opening", "lead_in", "achievements", "skills", "closing"],
            "properties": {
                "opening": {"type": "string", "minLength": 1, "maxLength": 1200},
                "lead_in": {"type": "string", "minLength": 1, "maxLength": 400},
                "achievements": {
                    "type": "array", "minItems": 4, "maxItems": 4,
                    "items": {"type": "object", "additionalProperties": False, "required": ["label", "text", "facts"],
                              "properties": {"label": {"type": "string", "minLength": 1, "maxLength": 60},
                                             "text": {"type": "string", "minLength": 1, "maxLength": 600},
                                             "facts": _FACT_IDS}}},
                "skills": {"type": "string", "minLength": 1, "maxLength": 1200},
                "closing": {"type": "string", "minLength": 1, "maxLength": 800},
            },
        },
    },
}

CV = {
    "type": "object",
    "additionalProperties": False,
    "required": ["variant", "summary", "experience"],
    "properties": {
        "variant": {"type": "string", "minLength": 1, "maxLength": 50},        # a key of profile/variants.yaml; checked on import
        "summary": {"type": "string", "minLength": 1, "maxLength": 1200},
        # selection, order and wording only: employer, title and dates of an experience come from master_cv.yaml by id
        "experience": {
            "type": "array", "minItems": 1, "maxItems": 12,
            "items": {"type": "object", "additionalProperties": False, "required": ["experience_id", "bullets"],
                      "properties": {"experience_id": _ID,
                                     "bullets": {"type": "array", "maxItems": 10,
                                                 "items": {"type": "object", "additionalProperties": False,
                                                           "required": ["fact_id", "text"],
                                                           "properties": {"fact_id": _ID,
                                                                          "text": {"type": "string", "minLength": 1, "maxLength": 400}}}}}}},
        # ids of the skill groups of master_cv.yaml (`skills-...`), in the order to show them; omitted: all groups
        "skills": {"type": "array", "items": _ID, "maxItems": 10, "uniqueItems": True},
    },
}

DOCUMENTS = {
    "type": "object",
    "additionalProperties": False,
    "minProperties": 1,
    "properties": {"cover_letter": COVER_LETTER, "cv": CV},
}

SCORE_ENTRY = {
    "type": "object",
    "additionalProperties": False,
    "required": ["job_id", "overall", "subscores", "matches", "gaps", "red_flags", "confidence", "summary"],
    "properties": {
        "job_id": {"type": "integer", "minimum": 1},
        "overall": _SCORE,
        "subscores": {"type": "object", "additionalProperties": False, "required": list(SUBSCORES),
                      "properties": {name: _SCORE for name in SUBSCORES}},
        "matches": _TEXTS,
        "gaps": _TEXTS,
        "red_flags": _TEXTS,
        "confidence": {"enum": ["low", "medium", "high"]},
        "summary": {"type": "string", "minLength": 1, "maxLength": 300},
        "cv_variant": {"type": ["string", "null"]},     # a key of profile/variants.yaml; checked on import
        "documents": DOCUMENTS,                         # schema 1.1 only; for a job in the top places above the cut-off
    },
}

ERROR_ENTRY = {
    "type": "object",
    "additionalProperties": False,
    "required": ["job_id", "error"],
    "properties": {"job_id": {"type": "integer", "minimum": 1}, "error": {"type": "string", "minLength": 1}},
}

RESULTS_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Recruitment results file",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "run_id", "model", "prompt_version", "results", "top"],
    "properties": {
        "schema_version": {"enum": list(SUPPORTED_VERSIONS)},
        "run_id": {"type": "string", "minLength": 1, "maxLength": 100},
        "model": {"type": "string", "minLength": 1},
        "prompt_version": {"type": "string", "minLength": 1},
        # One entry per job: a score record, or an error for a job that could not be scored.
        "results": {"type": "array", "items": {"oneOf": [SCORE_ENTRY, ERROR_ENTRY]}},
        # The session's own top-10 choice (job ids, best first). Advisory: the NAS applies goal.md 5.5 itself.
        "top": {"type": "array", "items": {"type": "integer", "minimum": 1}, "uniqueItems": True},
    },
}

_FILTER_RESULT = {
    "type": "object",
    "required": ["filter", "outcome", "reason_code", "comment"],
    "properties": {"filter": {"type": "string"}, "outcome": {"enum": ["pass", "fail", "conditional"]},
                   "reason_code": {"type": ["string", "null"]}, "comment": {"type": ["string", "null"]}},
}

BATCH_JOB = {
    "type": "object",
    "required": ["job_id", "fresh", "sources", "title", "company", "locations", "description", "description_missing",
                 "repost", "filter_results", "conditional_reasons"],
    "properties": {
        "job_id": {"type": "integer", "minimum": 1},
        "fresh": {"type": "boolean"},
        "sources": {"type": "array", "items": {"type": "object", "required": ["source", "external_id"]}},
        "title": {"type": "string"},
        "company": {"type": "string"},
        "locations": {"type": "array", "items": {"type": "string"}},
        "description": {"type": ["string", "null"]},
        "description_missing": {"type": "boolean"},
        "repost": {"type": "boolean"},
        "filter_results": {"type": "array", "items": _FILTER_RESULT},
        "conditional_reasons": {"type": "array", "items": {"type": "string"}},
        # true: the job was scored before and is scored again because a description was added
        "reevaluation": {"type": "boolean"},
        "previous_score": {"type": ["integer", "null"]},
        # additive, optional (v0.41.0): present and false when the code detected a posting written in German
        "posting_language_english": {"type": "boolean"},
    },
}

BATCH_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Recruitment batch file",
    "type": "object",
    "required": ["schema_version", "run_id", "created_at", "prompt", "profile", "schemas", "jobs"],
    "properties": {
        "schema_version": {"enum": list(SUPPORTED_VERSIONS)},
        "run_id": {"type": "string", "minLength": 1},
        "created_at": {"type": "string"},
        "prompt": {"type": "object", "required": ["version", "text"],
                   "properties": {"version": {"type": "string", "minLength": 1}, "text": {"type": "string", "minLength": 1}}},
        "profile": {"type": "object", "required": ["master_cv", "variants"]},
        "schemas": {"type": "object", "required": ["results"]},
        # schema 1.1: the rule for tailored documents, so the session knows which jobs need them
        "tailoring": {"type": "object", "required": ["documents_top_n", "min_score"],
                      "properties": {"documents_top_n": {"type": "integer", "minimum": 0},
                                     "min_score": {"type": "integer", "minimum": 0, "maximum": 100},
                                     "require_description": {"type": "boolean"}}},      # additive, optional (v0.40.0)
        # additive, optional (v0.41.0): the caps of the rubric, so the session knows them (must match the prompt)
        "scoring": {"type": "object",
                    "properties": {"no_description_cap": {"type": "integer", "minimum": 0, "maximum": 100},
                                   "non_english_cap": {"type": "integer", "minimum": 0, "maximum": 100}}},
        "jobs": {"type": "array", "items": BATCH_JOB},
    },
}


def message(e) -> str:
    """The message of a violation; a list or text that is too long or short is described without quoting it."""
    if e.validator in ("minItems", "maxItems") and isinstance(e.instance, list):
        return f"needs {'at least' if e.validator == 'minItems' else 'at most'} {e.validator_value} item(s), has {len(e.instance)}"
    if e.validator in ("minLength", "maxLength") and isinstance(e.instance, str):
        return f"text needs {'at least' if e.validator == 'minLength' else 'at most'} {e.validator_value} characters, has {len(e.instance)}"
    return e.message[:120]


def problems(data) -> list[tuple[str, str]]:
    """(path, message) for every schema violation of a results file. A record of `results` that fits neither a score nor an error
    entry is described by the violations of the branch it was meant for, so the reason names the field (for
    example `results/0/documents/cover_letter/blocks/achievements`) and not the whole record. (Moved here from app/results.py
    in v0.78.2: the headless worker drops single entries by these paths.)"""
    out: list[tuple[str, str]] = []
    for e in sorted(Draft202012Validator(RESULTS_SCHEMA).iter_errors(data), key=lambda e: list(map(str, e.path))):
        if e.validator == "oneOf" and e.context and isinstance(e.instance, dict):
            branch = 1 if "error" in e.instance else 0
            sub = [c for c in e.context if c.schema_path[0] == branch]
            if sub:
                out += [("/".join(map(str, c.absolute_path)) or "file", message(c)) for c in sub]
                continue
        out.append(("/".join(map(str, e.path)) or "file", message(e)))
    return out
