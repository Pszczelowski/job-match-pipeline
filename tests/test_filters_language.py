import pytest

from app import db
from app.config import ConfigError, load_config
from app.dedup import ingest_job
from app.filters.base import JobContext
from app.filters.language import assess, language
from app.filters.runner import run_filters
from app.models import NormalizedJob

C1, BELOW, UNSPEC, OPT = ("language_c1_required", "language_required_below_c1",
                          "language_level_unspecified", "language_optional")


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    return load_config()


def read(cfg, text):
    """{language: reason code} for a piece of text."""
    return {r.language: r.code for r in assess(text, cfg)}


# --- the levels (goal.md 5.3.2) ---------------------------------------------------------------

@pytest.mark.parametrize("text", ["German C1", "German (C2)", "C1 German", "Niveau C1 Deutsch", "C1-Niveau in German",
                                  "Deutsch auf Niveau C1", "German: C1 or higher"])
def test_c1_and_c2_reject(cfg, text):
    assert read(cfg, text) == {"German": C1}


@pytest.mark.parametrize("level", ["B2", "B1", "A2", "A1"])
def test_levels_below_c1_are_conditional(cfg, level):
    assert read(cfg, f"German {level}") == {"German": BELOW}
    assert read(cfg, f"Sprachniveau {level} Deutsch") == {"German": BELOW}


@pytest.mark.parametrize("text", ["fluent German", "Business fluent German", "German fluent", "verhandlungssicheres Deutsch",
                                  "Deutsch fliessend in Wort und Schrift", "Deutsch fließend", "native German speaker",
                                  "Fluent in English and German", "Fluency in German is essential",
                                  "Ability to communicate fluently in English and German"])
def test_fluent_type_phrases_reject(cfg, text):
    assert read(cfg, text) == {"German": C1}


@pytest.mark.parametrize("text", ["German required", "Good German", "Good knowledge of German", "German skills",
                                  "Gute Deutschkenntnisse", "must speak German", "German is a must"])
def test_required_without_level_is_conditional(cfg, text):
    assert read(cfg, text) == {"German": UNSPEC}


@pytest.mark.parametrize("text", ["German is a plus", "German is nice to have", "German is an advantage", "German preferred",
                                  "Deutsch von Vorteil", "Knowledge of German would be an asset"])
def test_optional_language_passes_with_note(cfg, text):
    assert read(cfg, text) == {"German": OPT}


def test_plus_wins_over_a_level(cfg):
    assert read(cfg, "German C1 is a plus") == {"German": OPT}


# --- ranges and spellings ----------------------------------------------------------------------

@pytest.mark.parametrize("text", ["German B2-C1", "German B2/C1", "German B2 to C1", "German level B2 - C1", "German B2–C1"])
def test_level_range_is_read_as_its_minimum(cfg, text):
    assert read(cfg, text) == {"German": BELOW}


def test_range_with_both_levels_below_c1(cfg):
    assert read(cfg, "German A2-B1") == {"German": BELOW}


@pytest.mark.parametrize("text", ["German Niveau B2", "German-Niveau B2", "German at level B2", "B2 level German"])
def test_level_spellings(cfg, text):
    assert read(cfg, text) == {"German": BELOW}


def test_lowercase_a1_and_b2b_are_not_levels(cfg):
    assert read(cfg, "Fluent German in room a1") == {"German": C1}          # still fluent, level token ignored
    assert read(cfg, "German B2B sales") == {}                              # no cue, no level: not a requirement


# --- several languages in one text -------------------------------------------------------------

def test_each_language_gets_its_own_level(cfg):
    assert read(cfg, "German C1 and French B2") == {"German": C1, "French": BELOW}
    assert read(cfg, "English (C1) and German (B2)") == {"German": BELOW}


def test_plus_applies_to_the_whole_chain(cfg):
    assert read(cfg, "French and German are a plus") == {"French": OPT, "German": OPT}


def test_plus_for_one_language_required_for_the_other(cfg):
    assert read(cfg, "German required, French a plus") == {"German": UNSPEC, "French": OPT}


def test_fluent_before_a_chain_covers_the_chain(cfg):
    assert read(cfg, "Fluent in German and French") == {"German": C1, "French": C1}


def test_fluent_english_does_not_leak_to_german(cfg):
    assert read(cfg, "Fluent English, good German") == {"German": UNSPEC}
    assert read(cfg, "Fluent English and Polish") == {}


def test_strongest_reading_per_language_wins(cfg):
    assert read(cfg, "German required.\nGerman C1 is expected as written level.\nGerman is a plus.") == {"German": C1}


@pytest.mark.parametrize("text, lang", [("Italian B2", "Italian"), ("Dutch required", "Dutch"), ("Französisch fliessend", "French"),
                                        ("good Spanish skills", "Spanish"), ("Français courant", "French")])
def test_other_languages_follow_the_same_rule(cfg, text, lang):
    assert lang in read(cfg, text)


def test_polish_and_english_never_matter(cfg):
    assert read(cfg, "Fluent English and Polish required, C1 level") == {}
    assert read(cfg, "English C2 mandatory") == {}


# --- text that is not a requirement ------------------------------------------------------------

@pytest.mark.parametrize("text", ["We offer German lessons for all staff.", "Our offices in Germany and Austria.",
                                  "Founded in 1856, the German bank is listed in Frankfurt.", "Reporting to the Italian branch head."])
def test_mentions_without_requirement_are_ignored(cfg, text):
    assert read(cfg, text) == {}


def test_language_mentioned_only_in_company_description_is_not_a_requirement(cfg):
    text = "About us\nWe are a Swiss bank with clients across the German market.\nYour profile\nBachelor degree and 5 years of experience in banking."
    assert read(cfg, text) == {}


def test_requirement_found_in_the_profile_section(cfg):
    text = "About us\nWe are a Swiss bank.\nYour profile\n- 5 years of experience\n- Fluent English and German (C1)\n- SQL"
    assert read(cfg, text) == {"German": C1}


# --- the filter function: outcomes, comments, German posting -----------------------------------

def job(description="", title="Business Analyst"):
    return JobContext(id=1, title=title, company="Acme", description=description, location_text="")


def test_comment_quotes_the_passage_and_names_the_language(cfg):
    (r,) = language(job("Your profile:\nFluent German (C1) is required for this role."), cfg)
    assert (r.outcome, r.reason_code) == ("fail", C1)
    assert "German" in r.comment and "Fluent German (C1) is required" in r.comment


def test_several_languages_give_one_result_each(cfg):
    results = language(job("German C1 and French B2"), cfg)
    assert {(r.outcome, r.reason_code) for r in results} == {("fail", C1), ("conditional", BELOW)}


def test_no_requirement_in_english_text_passes_without_reason(cfg):
    (r,) = language(job("We are looking for an analyst with SQL experience and strong communication skills."), cfg)
    assert (r.outcome, r.reason_code) == ("pass", None)


def test_no_description_is_recorded_as_not_assessed(cfg):
    (r,) = language(job(""), cfg)
    assert (r.outcome, r.reason_code) == ("pass", "language_not_assessed")


def test_language_in_title_is_checked_even_without_description(cfg):
    (r,) = language(job("", title="Business Analyst (German C1)"), cfg)
    assert (r.outcome, r.reason_code) == ("fail", C1)


GERMAN_TEXT = ("Wir suchen eine erfahrene Business Analystin für unser Team in Zürich. Sie arbeiten mit den Fachbereichen "
               "und der IT zusammen und sind für die Analyse der Anforderungen verantwortlich. Wir bieten Ihnen eine "
               "spannende Aufgabe in einem Umfeld, das von Zusammenarbeit geprägt ist.")


def test_german_posting_without_stated_requirement_is_conditional(cfg):
    (r,) = language(job(GERMAN_TEXT), cfg)
    assert (r.outcome, r.reason_code) == ("conditional", "language_implied_by_posting")


def test_german_posting_with_stated_requirement_uses_that_requirement(cfg):
    results = language(job(GERMAN_TEXT + "\nSehr gute Deutschkenntnisse auf Niveau B2."), cfg)
    assert [(r.outcome, r.reason_code) for r in results] == [("conditional", BELOW)]


def test_german_posting_with_preferred_german_is_optional_not_implied(cfg):
    results = language(job(GERMAN_TEXT + "\nDeutsch von Vorteil."), cfg)
    assert [(r.outcome, r.reason_code) for r in results] == [("pass", OPT)]


def test_english_posting_is_not_flagged_as_german(cfg):
    (r,) = language(job("We are looking for an experienced analyst to join our team in Zurich and work with the business and IT "
                        "on the analysis of requirements. You will be responsible for the delivery of the project."), cfg)
    assert r.reason_code is None


def test_short_german_snippet_is_not_enough(cfg):
    (r,) = language(job("Sie arbeiten mit dem Team."), cfg)
    assert r.reason_code is None


# --- config -------------------------------------------------------------------------------------

def test_phrase_list_change_takes_effect(cfg):
    assert read(cfg, "German mastery") == {}
    cfg["languages"]["c1_phrases"].append("mastery")
    assert read(cfg, "German mastery") == {"German": C1}


def test_new_language_can_be_added(cfg):
    cfg["languages"]["names"].append({"language": "Czech", "patterns": ["czech"]})
    assert read(cfg, "Czech C1") == {"Czech": C1}


def test_bad_language_config_is_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("languages:\n  c1_phrases: ['(']\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid regular expression"):
        load_config(p)
    p.write_text("languages:\n  names: [{language: X}]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="languages.names"):
        load_config(p)


# --- runner -------------------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def add(conn, description, **kw):
    base = dict(source="s", external_id="1", title="Business Analyst", company="Acme", url="", locations=["Zürich"],
                description=description)
    base.update(kw)
    return ingest_job(conn, NormalizedJob(**base)).job_id


def test_c1_requirement_archives_the_job_with_the_language_reason(conn, cfg):
    jid = add(conn, "Your profile: fluent German is a must.")
    assert run_filters(conn, jid, cfg) == "fail"
    row = conn.execute("SELECT reason_code, comment, rejection_stage FROM job_archive").fetchone()
    assert (row["reason_code"], row["rejection_stage"]) == (C1, "filter")
    assert "German" in row["comment"]


def test_conditional_language_keeps_the_job_and_stores_the_reason(conn, cfg):
    jid = add(conn, "Good German skills required.")
    assert run_filters(conn, jid, cfg) == "conditional"
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    row = conn.execute("SELECT outcome, reason_code FROM job_filter_results WHERE filter='language'").fetchone()
    assert tuple(row) == ("conditional", UNSPEC)


def test_worst_outcome_wins_across_languages(conn, cfg):
    jid = add(conn, "German B2 and French C1")
    assert run_filters(conn, jid, cfg) == "fail"
    assert conn.execute("SELECT reason_code FROM job_archive").fetchone()[0] == C1


# --- found on real UBS postings (31 Oct 2026) --------------------------------------------------------------

def test_fluent_turkish_is_c1(cfg):
    assert read(cfg, "• languages skills: fluent in Turkish and English") == {"Turkish": C1}


def test_a_clause_with_its_own_verb_is_not_part_of_the_earlier_list(cfg):
    # the "plus" belongs to German only; Spanish is fluent
    assert read(cfg, "• languages skills: fluent in Spanish and English, German is a plus") == {"Spanish": C1, "German": OPT}
    assert read(cfg, "English, German is a plus") == {"German": OPT}


def test_lists_without_a_verb_stay_together(cfg):
    assert read(cfg, "French, German and Italian are a plus") == {"French": OPT, "German": OPT, "Italian": OPT}
    assert read(cfg, "Fluent in English, German and French") == {"German": C1, "French": C1}


@pytest.mark.parametrize("text", [
    "Do you have a strong track record in advising and sales within the Spanish market(s)?",
    "in depth market knowledge in Türkiye with a convincing network of High and Ultra high clients",
    "We serve German clients and the German market from our Italian branch and the French team.",
    "Experience with the Swiss, German and Italian markets is helpful.",
])
def test_a_place_or_people_named_after_a_language_is_not_a_requirement(cfg, text):
    assert read(cfg, text) == {}


def test_german_speaking_clients_is_still_a_requirement(cfg):
    assert read(cfg, "German speaking clients") == {"German": UNSPEC}


@pytest.mark.parametrize("language, text", [("Arabic", "fluent Arabic"), ("Japanese", "Japanese C1"), ("Swedish", "fluent in Swedish and English"),
                                            ("Czech", "Czech required"), ("Ukrainian", "fluent in Ukrainian")])
def test_more_languages_are_watched(cfg, language, text):
    assert language in read(cfg, text)


def test_the_noun_list_is_configurable(cfg):
    cfg["languages"]["descriptive_nouns"].remove("markets?")
    assert read(cfg, "good Spanish markets knowledge") == {"Spanish": UNSPEC}


# --- sentences from the first eight real UBS postings (31 Oct 2026) ------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("• fluency in English is required;", {}),
    ("• fluency in Mandarin is highly advantageous but not mandatory;", {"Chinese": OPT}),
    ("• languages skills: fluent in English, other language skills are a plus.", {}),
    ("• languages skills: fluent in English and one of the languages spoken in the focus region", {}),
    ("• Excellent written and verbal English skills; additional languages are an advantage", {}),
    ("Full professional proficiency in German and English; French and/or Italian language skills are a strong advantage",
     {"German": C1, "French": OPT, "Italian": OPT}),
    ("• a good command of the German language is required", {"German": UNSPEC}),
])
def test_real_posting_lines(cfg, text, expected):
    assert read(cfg, text) == expected


@pytest.mark.parametrize("text", ["fluent in German, not required", "German is helpful", "French would be beneficial",
                                  "Italian is welcome", "Spanish not mandatory", "Dutch is not essential"])
def test_more_ways_to_say_optional(cfg, text):
    assert list(read(cfg, text).values()) == [OPT]


def test_full_professional_proficiency_is_read_as_c1(cfg):
    assert read(cfg, "Full professional proficiency in German") == {"German": C1}


@pytest.mark.parametrize("text", ["Experience with the Swiss, German and Italian markets is helpful.",
                                  "the German, French and Italian teams", "our German and Italian clients"])
def test_a_list_of_languages_before_a_noun_describes_the_noun(cfg, text):
    assert read(cfg, text) == {}


def test_a_list_before_speaking_is_still_a_requirement(cfg):
    assert read(cfg, "German and Italian speaking clients") == {"German": UNSPEC, "Italian": UNSPEC}


# --- "A and B, C or D is an advantage" (31 Oct 2026, SIX Technical Integration Manager) -------------

def test_an_advantage_for_a_short_list_does_not_cover_the_languages_before_it(cfg):
    text = "fluent German and English, French or Italian is an advantage."
    assert read(cfg, text) == {"German": C1, "French": OPT, "Italian": OPT}


@pytest.mark.parametrize("text, expected", [
    ("Good German and English, French or Italian is a plus.", {"German": UNSPEC, "French": OPT, "Italian": OPT}),
    ("Fluent in German, French is an advantage.", {"German": C1, "French": OPT}),                  # the single-language cut of v0.22.0
    ("Fluent in Spanish and English, German is a plus.", {"Spanish": C1, "German": OPT}),          # likewise
    ("German and French are a plus.", {"German": OPT, "French": OPT}),                              # one list without a comma
    ("English and German, Spanish or Portuguese is a plus.", {"Spanish": OPT, "Portuguese": OPT}),  # German has no requirement cue here
])
def test_short_lists_before_a_verb(cfg, text, expected):
    assert read(cfg, text) == expected


# --- a level shared by a joined group (fix 2 Nov 2026, postings 178 and 179) ------------------

@pytest.mark.parametrize("text, expected", [
    ("Du hast sehr gute Deutsch- und Englischkenntnisse (jeweils C1 Level)", {"German": C1}),        # posting 178
    ("Sprachen: Du verfügst über fliessende Deutschkenntnisse in Wort und Schrift.", {"German": C1}),  # posting 179
    ("German and English, both at C1", {"German": C1}),
    ("Deutsch und Englisch (C1)", {"German": C1}),
    ("German and French, each at B2", {"German": BELOW, "French": BELOW}),
    ("Deutsch B1, Englisch C1", {"German": BELOW}),                    # own level: no sharing
    ("Deutsch (B1) und Englisch (C1)", {"German": BELOW}),             # parentheses, but German has its own level
    ("English C1", {}),                                                # ordinary posting, nothing watched
    ("German C1 and French B2", {"German": C1, "French": BELOW}),
])
def test_shared_level_of_a_joined_group(cfg, text, expected):
    assert read(cfg, text) == expected


def test_very_good_german_without_level_stays_conditional(cfg):
    assert read(cfg, "Sehr gute Deutschkenntnisse") == {"German": UNSPEC}
    assert read(cfg, "sehr gutes Deutsch") == {"German": UNSPEC}


def test_native_level_wording_is_c1(cfg):
    assert read(cfg, "Deutsch auf Muttersprachniveau, gutes Englisch; Französisch ist ein Plus") == {"German": C1, "French": OPT}
