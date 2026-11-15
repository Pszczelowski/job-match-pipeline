"""v0.85.0: the home address is scrubbed from a revision request before it leaves the recruitment container (goal.md 5.10a).
Made-up address; no network, no database."""
import json

import pytest

from app.documents import ADDRESS_MARKER, _address_findings, address_trace, scrub_address

HOME = "Rosenweg 12, 8180 Exampletown"

VARIANTS = [
    "I live at Rosenweg 12, 8180 Exampletown and commute.",
    "I live at ROSENWEG 12, 8180 Exampletown and commute.",
    "I live at Rosenweg 12, 8180 Exampletown and commute.",
    "I live at rosenweg 12 8180 Exampletown and commute.",
    "I live at Rosenweg,, 12 ,, 8180   Exampletown and commute.",
    "I live at Rosenweg 12,\n8180 Exampletown and commute.",
    "I live at Rosenweg\n12 8180 Exampletown and commute.",
]


@pytest.mark.parametrize("text", VARIANTS)
def test_every_spelling_the_check_detects_is_gone(text):
    assert _address_findings({"x": text}, HOME)                       # the check sees it
    out, n = scrub_address({"x": text}, HOME)
    assert n == 1 and ADDRESS_MARKER in out["x"]
    assert not _address_findings(out, HOME) and not address_trace(json.dumps(out, ensure_ascii=False), HOME)
    assert out["x"].startswith("I live at ") and out["x"].endswith(" and commute.")


def test_the_street_part_alone_is_removed_and_the_town_alone_is_kept():
    out, n = scrub_address(["Near Rosenweg 12 in the evening", "A CV fact: I worked in Exampletown for a bank."], HOME)
    assert n == 1 and out[0] == f"Near {ADDRESS_MARKER} in the evening"
    assert out[1] == "A CV fact: I worked in Exampletown for a bank."


def test_the_count_is_the_number_of_spans_over_the_whole_structure():
    obj = {"a": [f"{HOME}.", {"b": f"x {HOME} y {HOME}"}], "n": 5, "ok": None, "t": True}
    out, n = scrub_address(obj, HOME)
    assert n == 3 and out["n"] == 5 and out["ok"] is None and out["t"] is True
    assert out["a"][0] == f"{ADDRESS_MARKER}."


def test_text_without_the_address_is_unchanged_byte_for_byte():
    obj = {"a": "Zürich, Rosen 12 and weg; 8180", "b": ["Exampletown", "  odd \t spacing\r\n"]}
    out, n = scrub_address(obj, HOME)
    assert n == 0 and out == obj and json.dumps(out) == json.dumps(obj)


@pytest.mark.parametrize("home", ["", "   ", "Ab 1", None])
def test_a_short_or_empty_address_scrubs_nothing(home):
    obj = {"a": "Ab 1 and more"}
    out, n = scrub_address(obj, home)
    assert n == 0 and out == obj


def test_the_trace_guard_sees_raw_folded_and_street_forms():
    assert address_trace("x Rosenweg 12 y", HOME) and address_trace("x ROSENWEG 12 y", HOME)
    assert address_trace("x rosenweg 12, 8180 Exampletown", HOME)
    assert not address_trace("Exampletown and Rosen weg", HOME)
    assert not address_trace("anything", "")


def test_a_scrubbed_marker_is_not_scrubbed_again():
    once, n1 = scrub_address([HOME], HOME)
    twice, n2 = scrub_address(once, HOME)
    assert n1 == 1 and n2 == 0 and twice == once


def test_an_address_split_by_invisible_formatting_characters_is_scrubbed():
    text = "I live at Rosen\u00adweg 12,\u200b 8180 Example\u200dtown."
    assert _address_findings({"x": text}, HOME)
    out, n = scrub_address({"x": text}, HOME)
    assert n == 1 and ADDRESS_MARKER in out["x"] and not _address_findings(out, HOME)
    assert address_trace("Rosen\u00adweg 12", HOME) and not address_trace(out["x"], HOME)
