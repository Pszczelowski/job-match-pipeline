"""Line endings of CV bullets and letter paragraphs (v0.57.0): the measured check in app/pdf.py."""
import yaml

from app import pdf
from app.documents import ROOT

OLD_TESTING = "Testing Management: Wrote detailed test cases and executed End-to-End UAT processes within Xray to ensure platform stability."
OLD_SSRS = ("SSRS Reporting: Built and maintained SQL Server Reporting Services reports for the Daily Calculation project, used by the "
            "Operations and Asset Management teams; added features per requirements, wrote and executed unit and regression tests, "
            "and reduced errors.")
ONE_LINE = "Requirements Gathering: Worked with IT and Operations on the Fund Platform."


def flags(text, kind="cv_bullet"):
    return pdf.line_ending_flags([("x", kind, text)])


def test_the_user_examples_are_flagged_with_their_last_line():
    testing = flags(OLD_TESTING)
    assert [f["last_line"] for f in testing] == ["stability."] and testing[0]["words"] == 1
    ssrs = flags(OLD_SSRS)
    assert [f["last_line"] for f in ssrs] == ["reduced errors."] and ssrs[0]["lines"] == 3 and ssrs[0]["fill"] < 0.35


def test_a_block_that_fits_one_line_is_never_flagged():
    assert flags(ONE_LINE) == [] and flags("Short.") == []


def test_a_well_filled_last_line_is_not_flagged():
    base = "Delivered reliable reporting for the operations teams and careful work with colleagues"
    found_ok = False
    for n in range(1, 30):
        t = base + " " + " ".join(["and more steady work"] * n)
        found = flags(t)
        if not found:
            found_ok = True
        else:
            assert found[0]["words"] < 3 or found[0]["fill"] < 0.35
    assert found_ok


def test_three_words_but_under_a_third_of_the_line_is_flagged():
    base = "Delivered reliable reporting for the operations teams and more careful work with colleagues across sites"
    for n in range(1, 40):
        t = " ".join([base] * n) + " on a day"
        found = flags(t)
        if found:
            assert found[0]["words"] >= 3 or found[0]["fill"] < 0.35
            assert found[0]["fill"] < 0.35 or found[0]["words"] < 3
            return
    raise AssertionError("no stub found")


def test_the_check_uses_the_renderer_width_of_the_block_kind():
    # a text that fills almost the whole line of a summary is two lines at the narrower bullet width
    summary_doc, summary_w, _ = pdf._measure("cv_summary")
    bullet_doc, bullet_w, _ = pdf._measure("cv_bullet")
    assert abs((summary_w - bullet_w) - pdf.CV_BULLET_INDENT) < 0.01
    letter_doc, letter_w, _ = pdf._measure("letter_paragraph")
    assert abs(letter_w - (210 - 2 * pdf.LETTER_MARGIN_X)) < 0.01
    assert abs(pdf._measure("letter_achievement")[1] - (letter_w - 12)) < 0.01
    text = "word word word"
    while summary_doc.get_string_width(text) <= bullet_w + 0.3:
        text += " i"
    assert bullet_w < summary_doc.get_string_width(text) < summary_w
    assert flags(text, "cv_summary") == []                           # one line at the summary width
    assert flags(text, "cv_bullet") != []                            # a stub at the bullet width


def test_the_flag_agrees_with_the_real_wrapping_of_the_renderer():
    doc, width, height = pdf._measure("cv_bullet")
    _t, body, markdown = pdf._bullet_parts(OLD_TESTING)
    from fpdf.enums import MethodReturnValue
    lines = doc.multi_cell(width, height, body, align="J", markdown=markdown, dry_run=True, output=MethodReturnValue.LINES)
    assert len(lines) == flags(OLD_TESTING)[0]["lines"] == 2


def test_letter_paragraphs_and_achievements_are_checked_too():
    doc, width, _ = pdf._measure("letter_paragraph")
    text = ""
    while doc.get_string_width(text) < width * 1.05:
        text += "word "
    stub = flags(text.strip() + " end", "letter_paragraph")
    assert stub and stub[0]["last_line"].endswith("end") and stub[0]["fill"] < 0.35
    assert flags("Short paragraph.", "letter_paragraph") == []
    assert flags("Label: " + text.strip() + " end", "letter_achievement")


def test_the_report_for_a_results_file_lists_the_flagged_blocks(tmp_path):
    import json
    cv = {"variant": "resume", "summary": "A short summary.",
          "experience": [{"experience_id": "exp-contoso", "bullets": [{"fact_id": "a", "text": OLD_TESTING}, {"fact_id": "b", "text": ONE_LINE}]}]}
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"results": [{"job_id": 7, "documents": {"cv": cv}}]}), encoding="utf-8")
    report = pdf.line_endings_report(path)
    assert "job 7 cv experience 1 (exp-contoso), bullet 1" in report and "stability." in report and "bullet 2" not in report
    path.write_text(json.dumps({"results": [{"job_id": 7, "documents": {"cv": {**cv, "experience": [{"experience_id": "e", "bullets": [{"fact_id": "b", "text": ONE_LINE}]}]}}}]}), encoding="utf-8")
    assert pdf.line_endings_report(path) == "no line-ending problems"


def test_the_fact_file_has_no_bullet_or_summary_with_a_stub_last_line():
    master = yaml.safe_load((ROOT / "profile" / "master_cv.yaml").read_text(encoding="utf-8"))
    variants = yaml.safe_load((ROOT / "profile" / "variants.yaml").read_text(encoding="utf-8"))
    blocks = [(f"{b['id']}/{key}", "cv_bullet", text) for e in master["experience"] for b in e["bullets"]
              for key, text in b.get("texts", {}).items() if key in ("resume", "resume_cs")]
    blocks += [(f"summary/{k}", "cv_summary", " ".join(v["summary"].split())) for k, v in variants["variants"].items()]
    assert len(blocks) > 50
    assert pdf.line_ending_flags(blocks) == []


def test_the_font_setup_runs_once_per_kind_however_many_blocks_and_documents(monkeypatch):
    monkeypatch.setattr(pdf, "_MEASURED", {})
    made = []
    real = pdf._Pdf.__init__

    def counting(self, *a, **kw):
        made.append(1)
        real(self, *a, **kw)
    monkeypatch.setattr(pdf._Pdf, "__init__", counting)
    text = "Led a team of engineers through the migration of a large payment platform to a new cloud setup in time"
    blocks = [("b", kind, text) for kind in ("cv_bullet", "cv_summary", "letter_paragraph", "letter_achievement")] * 25
    first = pdf.line_ending_flags(blocks)
    for _ in range(5):
        assert pdf.line_ending_flags(blocks) == first
    assert len(made) <= 4
    monkeypatch.setattr(pdf, "_MEASURED", {})
    assert pdf.line_ending_flags(blocks) == first


def test_the_letter_measure_wraps_like_the_renderer_without_an_inner_cell_margin():
    """render_cover_letter sets c_margin = 0; the measuring document of the letter kinds must wrap the same way (v0.79.1)."""
    from fpdf.enums import MethodReturnValue
    for kind in ("letter_paragraph", "letter_achievement"):
        measuring, width, height = pdf._measure(kind)
        assert measuring.c_margin == 0
    assert pdf._measure("cv_bullet")[0].c_margin == pdf._Pdf("t", "t", pdf.date(2000, 1, 1)).c_margin
    measuring, width, height = pdf._measure("letter_paragraph")
    words = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu".split()
    differing = 0
    for n in range(40, 90):
        text = " ".join(words[(i * 7) % len(words)] + "x" * (i % 4) for i in range(n))
        real = pdf._Pdf("t", "t", pdf.date(2000, 1, 1))
        real.set_margins(pdf.LETTER_MARGIN_X, pdf.LETTER_MARGIN_TOP, pdf.LETTER_MARGIN_X)
        real.c_margin = 0
        real.add_page()
        real.set_font(pdf.FAMILY, "", pdf.LETTER_PT)
        want = real.multi_cell(width, height, text, align="J", dry_run=True, output=MethodReturnValue.LINES)
        got = measuring.multi_cell(width, height, text, align="J", dry_run=True, output=MethodReturnValue.LINES)
        differing += want != got
    assert differing == 0
