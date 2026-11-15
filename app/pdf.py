"""PDF rendering of the cover letter and the CV (goal.md 5.6, slices 3c and 3d).

Pure functions: stored document content (checked and stored by `app/documents.py`) plus the fact file go in, a PDF
file comes out. No database, no network, no config. fpdf2 with the bundled EB Garamond (`app/fonts/`, SIL OFL).

* Letter: A4, margins 18 mm left and right and 15 mm top and bottom (decided by the user on 1 Nov 2026), one contact
  line under the name, the generation date, the employer's name and city, the salutation ("Dear <contact>," or
  "Dear Hiring Manager,"), the session's blocks, "Thank you for your time and consideration.", "Sincerely,", the
  name. No street address.
* Justification: running text (letter paragraphs and achievements, CV summary, bullets and the languages line) is
  justified; the last line of a paragraph is not stretched. Headings, dates, the salutation, the signature and the
  skill lists (short items on one line each) are not running text and stay left-aligned.
* Sections of the CV stay together: a unit (the summary, the work-experience heading with the first job, every further
  job with its bullets, education with languages, the technical skills) is never split by a page break; one that does
  not fit on the rest of a page starts on the next one (`_keep_together`). Only a unit longer than a whole page may break.
* CV: A4, one layout for both variants (the two samples differ only in the heading above the summary: "SUMMARY" for
  `resume`, the variant's headline for `resume_cs`). Employer, title, place and dates come from the fact file by
  `experience_id`; education, certificates, languages and skill groups come from the fact file; summary and bullets
  come from the stored document. The photo is optional (a missing photo gives a CV without photo and a warning).
* Text stays selectable (embedded TrueType fonts). The creation date is the generation date, so the same input gives
  the same bytes.
* The home address never reaches a file: every drawn string is checked against `home_address`.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import MethodReturnValue

FONT_DIR = Path(__file__).resolve().parent / "fonts"
FAMILY = "EBGaramond"
BLUE = (17, 85, 204)          # 1155CC, the link colour of the samples
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
ABBR = ["Jan.", "Feb.", "Mar.", "Apr.", "May", "Jun.", "Jul.", "Aug.", "Sep.", "Oct.", "Nov.", "Dec."]

PAGE_WARNING = "pdf_over_page_limit"


class PdfError(ValueError):
    """A document cannot be rendered (for example the home address would be in it)."""


@dataclass
class Rendered:
    path: Path
    pages: int
    warnings: list[str] = field(default_factory=list)      # for example "no photo"


# ----------------------------------------------------------------------------- small helpers

def long_date(d: date) -> str:
    """'1 November 2026', independent of the locale."""
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def month_year(value) -> str:
    """'2025-07' -> 'Jul. 2025'; 'present' -> 'Present'; dates and plain years are accepted."""
    if isinstance(value, (date, datetime)):
        return f"{ABBR[value.month - 1]} {value.year}"
    s = str(value).strip()
    if s.lower() == "present":
        return "Present"
    m = re.fullmatch(r"(\d{4})-(\d{2})(?:-\d{2})?", s)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{ABBR[int(m.group(2)) - 1]} {m.group(1)}"
    return s


def date_range(start, end) -> str:
    return f"{month_year(start)} – {month_year(end)}"


def birthdate_text(value) -> str:
    if isinstance(value, (date, datetime)):
        return f"{value.day:02d}.{value.month:02d}.{value.year}"
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", str(value).strip())
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else str(value)


_FOLD = str.maketrans({"ł": "l", "Ł": "L", "ß": "ss", "æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ı": "i"})


def ascii_fold(text: str) -> str:
    """Letters without accents ('Morgan' -> 'Morgan'); anything else that is not ASCII is dropped."""
    text = unicodedata.normalize("NFKD", text.translate(_FOLD))
    return text.encode("ascii", "ignore").decode("ascii")


def safe_name(text: str, limit: int = 60) -> str:
    """ASCII text that is safe in a file name on Windows and Linux."""
    s = re.sub(r"[^A-Za-z0-9 .,&()+-]", " ", ascii_fold(text))
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:limit].strip(" .") or "Company"


def slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", ascii_fold(text).lower()).strip("-")
    return s[:limit].strip("-") or "company"


def folder_name(job_id: int, company: str) -> str:
    return f"{job_id}_{slug(company)}"


def file_name(person_name: str, kind: str, company: str) -> str:
    label = "Cover Letter" if kind == "cover_letter" else "CV"
    return f"{safe_name(ascii_fold(person_name), 40)} - {label} - {safe_name(company)}.pdf"


_TITLES = {"ms", "mrs", "mr", "miss", "mx", "dr", "prof"}


def salutation_name(contact: str) -> str:
    """The name for "Dear ...," from the contact the posting gives.

    A contact that starts with one or more courtesy or academic titles (Ms, Mrs, Mr, Miss, Mx, Dr, Prof, with or
    without a dot, any case, also chained as "Prof. Dr.") is cut to the title(s), exactly as written, and the
    surname: the last word plus lower-case particles before it ("Mr Jan van der Berg" -> "Mr van der Berg",
    "Ms Anna Muster" -> "Ms Muster"). Without a title the full name is kept as written, because the gender is
    unknown and no title is invented. A contact that is only a title is returned unchanged.
    """
    words = contact.split()
    n = 0
    while n < len(words) and words[n].rstrip(".").lower() in _TITLES:
        n += 1
    rest = words[n:]
    if n == 0 or not rest:
        return " ".join(words)
    start = len(rest) - 1
    while start > 0 and rest[start - 1][:1].islower():
        start -= 1
    return " ".join(words[:n] + rest[start:])


def _clean(text) -> str:
    """Plain text for the page: no control characters, and none of fpdf2's markdown markers (**, __, --)."""
    s = "".join(ch for ch in str(text) if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    s = s.replace("\t", " ").replace("**", "").replace("__", "_").replace("--", "–")
    return re.sub(r"[  ]+", " ", s).strip()


def city_of(locations: list[str]) -> str:
    """The city for the line below the employer's name: the first place of the first location text."""
    for loc in locations or []:
        part = str(loc).split(",")[0].strip()
        if part:
            return part
    return ""


def label_split(text: str) -> tuple[str, str]:
    """('Label:', 'rest') when the text starts with a short label ending in a colon, else ('', text)."""
    i = text.find(": ")
    if 0 < i <= 55 and len(text[:i].split()) <= 6 and "." not in text[:i]:
        return text[:i + 1], text[i + 2:]
    return "", text


# ----------------------------------------------------------------------------- the PDF object

class _Pdf(FPDF):
    """FPDF with the bundled font, a record of every drawn string and a fixed creation date."""

    def __init__(self, title: str, author: str, generated_on: date, home_address: str = ""):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.drawn: list[str] = []
        self.home_address = home_address
        for style, file in (("", "Regular"), ("B", "Bold"), ("I", "Italic"), ("BI", "BoldItalic")):
            self.add_font(FAMILY, style, str(FONT_DIR / f"EBGaramond-{file}.ttf"))
        self.set_title(title)
        self.set_author(author)
        self.set_creator("Recruitment pipeline (fpdf2)")
        self.set_creation_date(datetime(generated_on.year, generated_on.month, generated_on.day, tzinfo=timezone.utc))
        self.set_compression(True)

    def note(self, *texts: str) -> None:
        self.drawn.extend(texts)

    def check_address(self) -> None:
        needle = " ".join(re.findall(r"\w+", self.home_address.lower()))
        if len(needle) < 5:
            return
        haystack = " ".join(re.findall(r"\w+", " ".join(self.drawn).lower()))
        if needle in haystack:
            raise PdfError("the rendered text contains the home address; nothing was written")


def _save(pdf: _Pdf, out_path: Path) -> int:
    pdf.check_address()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    pdf.output(str(tmp))
    tmp.replace(out_path)
    return pdf.page_no()


# ----------------------------------------------------------------------------- cover letter

LETTER_PT = 12
LETTER_LINE = 5.3          # mm
# Letter margins (decided by the user on 1 Nov 2026, goal.md 5.6: "I would like to use as much page as possible"):
# 18 mm left and right, 15 mm top and bottom. Constants and not config keys: the render functions take no config.
LETTER_MARGIN_X = 18.0     # mm
LETTER_MARGIN_TOP = 15.0   # mm
LETTER_MARGIN_BOTTOM = 15.0  # mm: where the automatic page break starts


def render_cover_letter(doc: dict, job: dict, facts: dict, generated_on: date, out_path, home_address: str = "") -> Rendered:
    """`doc`: the stored `cover_letter` content ({contact_name, blocks}); `job`: {company, locations, ...};
    `facts`: the parsed `profile/master_cv.yaml`. Returns the path and the page count."""
    person = facts["person"]
    name = person["name"]
    company = _clean(job.get("company") or "")
    contact = _clean(doc.get("contact_name") or "")
    b = doc["blocks"]
    pdf = _Pdf(f"{name} - Cover Letter - {company}", name, generated_on, home_address)
    pdf.set_margins(LETTER_MARGIN_X, LETTER_MARGIN_TOP, LETTER_MARGIN_X)
    pdf.set_auto_page_break(True, LETTER_MARGIN_BOTTOM)
    pdf.c_margin = 0                    # no extra inner margin of a cell: the text starts exactly at the page margin
    pdf.add_page()
    w = pdf.epw

    def para(text: str, gap: float = 3.6, markdown: bool = False, indent: float = 0.0) -> None:
        pdf.set_x(pdf.l_margin + indent)
        pdf.multi_cell(w - indent, LETTER_LINE, text, align="J", markdown=markdown, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(gap)

    # header: name, one contact line
    pdf.set_font(FAMILY, "B", 16)
    pdf.cell(0, 8, name, new_x="LMARGIN", new_y="NEXT")
    pdf.note(name)
    pdf.set_font(FAMILY, "", 10.5)
    parts = [("email", person["email"], f"mailto:{person['email']}"), ("phone", person["phone"], ""),
             ("linkedin", person["linkedin"], person["linkedin"])]
    for i, (_, text, link) in enumerate(parts):
        if i:
            pdf.write(5, "  |  ")
        if link:
            pdf.set_text_color(*BLUE)
        pdf.write(5, text, link=link)
        pdf.set_text_color(0, 0, 0)
        pdf.note(text)
    pdf.ln(11)

    pdf.set_font(FAMILY, "", LETTER_PT)
    # date, employer, city
    head = [long_date(generated_on)]
    pdf.cell(0, LETTER_LINE, head[0], new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3.6)
    for line in (company, city_of(job.get("locations") or [])):
        if line:
            pdf.cell(0, LETTER_LINE, line, new_x="LMARGIN", new_y="NEXT")
            pdf.note(line)
    pdf.ln(7)
    salutation = f"Dear {salutation_name(contact)}," if contact else "Dear Hiring Manager,"
    pdf.cell(0, LETTER_LINE, salutation, new_x="LMARGIN", new_y="NEXT")
    pdf.note(salutation)
    pdf.ln(3.6)

    for key in ("opening", "lead_in"):
        text = _clean(b[key])
        pdf.note(text)
        para(text, 3.0 if key == "lead_in" else 3.6)
    for a in b["achievements"]:
        label, text = _clean(a["label"]), _clean(a["text"])
        pdf.note(label, text)
        pdf.set_x(pdf.l_margin + 6)
        pdf.cell(5, LETTER_LINE, "•")
        pdf.set_x(pdf.l_margin + 12)
        pdf.set_font(FAMILY, "", LETTER_PT)
        pdf.multi_cell(w - 12, LETTER_LINE, f"**{label}:** {text}", align="J", markdown=True, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2.4)
    pdf.ln(1.2)
    for key in ("skills", "closing"):
        text = _clean(b[key])
        pdf.note(text)
        para(text)
    pdf.ln(0.5)
    pdf.cell(0, LETTER_LINE, "Thank you for your time and consideration.", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)
    pdf.cell(0, LETTER_LINE, "Sincerely,", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(7)
    pdf.cell(0, LETTER_LINE, name, new_x="LMARGIN", new_y="NEXT")
    return Rendered(Path(out_path), _save(pdf, out_path))


# ----------------------------------------------------------------------------- CV

CV_PT = 10.5
CV_LINE = 4.3              # mm
CV_BULLET_INDENT = 4.2
MARGIN_X = 11.4


def _cv_heading(pdf: _Pdf, text: str) -> None:
    if pdf.get_y() > pdf.t_margin + 0.01:                 # no empty gap above a heading at the top of a page
        pdf.ln(2.2)
    pdf.set_font(FAMILY, "B", CV_PT)
    pdf.cell(0, 5, text, new_x="LMARGIN", new_y="NEXT")
    pdf.note(text)
    y = pdf.get_y()
    pdf.set_line_width(0.4)
    pdf.line(pdf.l_margin, y, pdf.l_margin + pdf.epw, y)
    pdf.ln(2.4)


def _two_sides(pdf: _Pdf, left: str, right: str, left_style: str, right_style: str, h: float = CV_LINE) -> None:
    """One line with text at the left and at the right margin."""
    pdf.set_font(FAMILY, left_style, CV_PT)
    pdf.cell(pdf.epw - 55, h, left)
    pdf.set_font(FAMILY, right_style, CV_PT)
    pdf.cell(55, h, right, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.note(left, right)


def _bullet_parts(text: str, bold_label: bool = True) -> tuple[str, str, bool]:
    """(clean text, text to draw, whether it holds a bold label) of a bullet."""
    text = _clean(text)
    label, rest = label_split(text) if bold_label else ("", text)
    return text, (f"**{label}** {rest}" if label else text), bool(label)


def _bullet_height(pdf: _Pdf, text: str) -> float:
    """The height a bullet takes (measured, nothing is drawn)."""
    _text, body, markdown = _bullet_parts(text)
    pdf.set_font(FAMILY, "", CV_PT)
    return pdf.multi_cell(pdf.epw - CV_BULLET_INDENT, CV_LINE, body, align="J", markdown=markdown, dry_run=True,
                          output=MethodReturnValue.HEIGHT)


def _bullet(pdf: _Pdf, text: str, bold_label: bool = True, gap: float = 0.5) -> None:
    text, body, markdown = _bullet_parts(text, bold_label)
    pdf.note(text)
    pdf.set_font(FAMILY, "", CV_PT)
    if pdf.will_page_break(_bullet_height(pdf, text)):        # only inside a job longer than a page: a bullet is not cut in two
        pdf.add_page()
    pdf.set_x(pdf.l_margin + 1.2)
    pdf.cell(3, CV_LINE, "•")
    pdf.set_x(pdf.l_margin + CV_BULLET_INDENT)
    pdf.multi_cell(pdf.epw - CV_BULLET_INDENT, CV_LINE, body, align="J", markdown=markdown, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(gap)


def _wrap_items(pdf: _Pdf, items: list[str], width: float) -> list[str]:
    """Skill items joined with ' | ', as many per line as fit the width."""
    lines: list[str] = []
    cur = ""
    for item in items:
        trial = f"{cur} | {item}" if cur else item
        if cur and pdf.get_string_width(trial) > width:
            lines.append(cur)
            cur = item
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def render_cv(doc: dict, facts: dict, variants: dict, out_path, photo=None, generated_on: date | None = None,
              home_address: str = "") -> Rendered:
    """`doc`: the stored `cv` content; `facts`: the parsed `profile/master_cv.yaml`; `variants`: the parsed
    `profile/variants.yaml` (for the headline of the variant); `photo`: path of an image or None."""
    person = facts["person"]
    name = person["name"]
    generated_on = generated_on or date.today()
    pdf = _Pdf(f"{name} - CV", name, generated_on, home_address)
    pdf.set_margins(MARGIN_X, 9, MARGIN_X)
    pdf.set_auto_page_break(True, 9)
    pdf.add_page()
    warnings: list[str] = []
    experiences = {e["id"]: e for e in facts.get("experience", [])}

    # header: name and personal lines at the left, photo at the right
    top = pdf.get_y()
    pdf.set_font(FAMILY, "B", 18)
    pdf.cell(0, 8.5, name, new_x="LMARGIN", new_y="NEXT")
    pdf.note(name)
    pdf.set_font(FAMILY, "", CV_PT)
    lines = [(person["email"], f"mailto:{person['email']}"), (person["phone"], ""), (person["location"], ""),
             (f"Nationality: {person['nationality']}", ""), (f"Permit: {person['permit']}", ""),
             (f"Birthdate: {birthdate_text(person['birthdate'])}", ""), (person["linkedin"], person["linkedin"])]
    for text, link in lines:
        if link:
            pdf.set_text_color(*BLUE)
        pdf.cell(100, CV_LINE, text, link=link, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.note(text)
    header_bottom = pdf.get_y()
    photo_path = Path(photo) if photo else None
    if photo_path is not None and photo_path.is_file():
        try:
            pdf.image(str(photo_path), x=pdf.l_margin + pdf.epw - 42, y=top + 0.5, w=42, h=32.6, keep_aspect_ratio=False)
        except Exception as exc:                      # unreadable image: no photo, a warning
            warnings.append(f"photo {photo_path} could not be used ({exc}); the CV has no photo")
    else:
        warnings.append(f"photo not found ({photo_path}); the CV has no photo" if photo_path else "no photo configured; the CV has no photo")
    pdf.set_y(max(header_bottom, top + 34) + 2.5)

    # The sections are drawn one unit at a time and a unit never straddles a page break (`_keep_together`): the summary,
    # the work-experience heading with the first job, every further job with its bullets, education and languages as a
    # whole, and the technical skills as a whole. A unit that does not fit on the rest of the page starts on the next
    # one. Only a unit that is itself longer than a page (one job with very many bullets) may break, after it has
    # started at the top of a fresh page.
    probe = _probe(pdf)
    variant = (variants.get("variants") or {}).get(doc["variant"]) or {}
    summary = _clean(doc["summary"])

    def draw_summary(p: _Pdf) -> None:
        _cv_heading(p, _clean(variant.get("headline") or "") or "SUMMARY")      # the headline of the variant, else SUMMARY
        p.note(summary)
        p.set_font(FAMILY, "", CV_PT)
        p.multi_cell(p.epw, CV_LINE, summary, align="J", new_x="LMARGIN", new_y="NEXT")

    _keep_together(pdf, probe, draw_summary)

    # work experience
    entries = []
    for entry in doc["experience"]:
        exp = experiences.get(entry["experience_id"])
        if exp is None:
            raise PdfError(f"experience {entry['experience_id']} is not in the fact file")
        entries.append((exp, entry))
    bullet_text = [bullet["text"] for _exp, entry in entries for bullet in entry["bullets"]]
    if not entries:
        _keep_together(pdf, probe, lambda p: _cv_heading(p, "WORK EXPERIENCE"))
    for n, (exp, entry) in enumerate(entries):
        def draw_job(p: _Pdf, exp=exp, entry=entry, first=(n == 0)) -> None:
            if first:
                _cv_heading(p, "WORK EXPERIENCE")
            _cv_experience(p, exp, entry)

        _keep_together(pdf, probe, draw_job, tail=JOB_GAP)

    # Decided 2 Nov 2026: education and skills always start on page 2; page 1 holds the header, summary and experience.
    if pdf.page_no() == 1:
        pdf.add_page()

    # education, certificates, languages
    _keep_together(pdf, probe, lambda p: _cv_education(p, facts, bullet_text))

    # technical skills, two columns, groups in the stored order (else the file's order)
    groups = {g["id"]: g for g in facts.get("skills", [])}
    order = [g for g in (doc.get("skills") or []) if g in groups] or list(groups)
    if order:
        _keep_together(pdf, probe, lambda p: _cv_skills(p, groups, order, doc["variant"]), tail=SKILLS_GAP)
    pages = _save(pdf, out_path)
    return Rendered(Path(out_path), pages, warnings)


# ----------------------------------------------------------------------------- line endings (v0.57.0)

LINE_END_MIN_WORDS = 3         # the last line of a wrapped block holds at least this many words ...
LINE_END_MIN_FILL = 0.35       # ... and fills at least this share of the line width (single-line blocks are never flagged)
LINE_ENDING_WARNING = "line_ending"


def _measure(kind: str) -> tuple[_Pdf, float, float]:
    """(document, width, line height) set up exactly as the renderer sets up the block `kind`."""
    pdf = _Pdf("measure", "measure", date(2000, 1, 1))
    if kind.startswith("letter"):
        pdf.set_margins(LETTER_MARGIN_X, LETTER_MARGIN_TOP, LETTER_MARGIN_X)
        pdf.c_margin = 0                # as render_cover_letter: the text is wrapped at the full width
        pdf.add_page()
        pdf.set_font(FAMILY, "", LETTER_PT)
        return pdf, pdf.epw - (12 if kind == "letter_achievement" else 0), LETTER_LINE
    pdf.set_margins(MARGIN_X, 9, MARGIN_X)
    pdf.add_page()
    pdf.set_font(FAMILY, "", CV_PT)
    return pdf, pdf.epw - (CV_BULLET_INDENT if kind == "cv_bullet" else 0), CV_LINE


_MEASURED: dict[str, tuple[_Pdf, float, float]] = {}      # one document per kind for the whole process (v0.78.6)


def _measured(kind: str) -> tuple[_Pdf, float, float]:
    """`_measure(kind)`, built once per process: every new _Pdf keeps its fonts in fpdf2's glyph caches until the
    process ends (about 10 MB each), so a document per call used to run the llm container out of memory."""
    if kind not in _MEASURED:
        _MEASURED[kind] = _measure(kind)
    return _MEASURED[kind]


def line_ending_flags(blocks: list[tuple[str, str, str]]) -> list[dict]:
    """Blocks whose last line is a stub. `blocks`: (location, kind, text) with kind `cv_bullet`, `cv_summary`,
    `letter_paragraph` or `letter_achievement` (text as stored; for achievements 'label: text'). The lines are wrapped
    by fpdf2 with the font, size, width and bold label of the renderer. Returns dicts with `location`, `last_line`,
    `words`, `fill` (share of the line width) and `lines`. Flagged: fewer than 3 words or under 35 % of the width."""
    out: list[dict] = []
    for location, kind, text in blocks:
        pdf, width, height = _measured(kind)
        markdown = kind in ("cv_bullet", "letter_achievement")
        if kind == "cv_bullet":
            _t, body, markdown = _bullet_parts(text)
        elif kind == "letter_achievement":
            label, _, rest = text.partition(": ")
            body = f"**{_clean(label)}:** {_clean(rest)}"
        else:
            body = _clean(text)
        lines = pdf.multi_cell(width, height, body, align="J", markdown=markdown, dry_run=True, output=MethodReturnValue.LINES)
        if len(lines) < 2:
            continue
        last = lines[-1].replace("**", "").strip()
        words = len(last.split())
        fill = pdf.get_string_width(last) / width
        if words < LINE_END_MIN_WORDS or fill < LINE_END_MIN_FILL:
            out.append({"location": location, "last_line": last, "words": words, "fill": round(fill, 2), "lines": len(lines)})
    return out


def document_line_flags(kind: str, content: dict) -> list[dict]:
    """Line-ending flags of a stored `cover_letter` or `cv` content."""
    blocks: list[tuple[str, str, str]] = []
    if kind == "cover_letter":
        b = content["blocks"]
        blocks += [("opening", "letter_paragraph", b["opening"]), ("lead_in", "letter_paragraph", b["lead_in"])]
        blocks += [(f"achievement {i}", "letter_achievement", f"{a['label']}: {a['text']}")
                   for i, a in enumerate(b["achievements"], 1)]
        blocks += [("skills", "letter_paragraph", b["skills"]), ("closing", "letter_paragraph", b["closing"])]
    else:
        blocks.append(("summary", "cv_summary", content["summary"]))
        for i, exp in enumerate(content["experience"], 1):
            blocks += [(f"experience {i} ({exp['experience_id']}), bullet {j}", "cv_bullet", bl["text"])
                       for j, bl in enumerate(exp["bullets"], 1)]
    return line_ending_flags(blocks)


def line_ending_reason(flag: dict) -> str:
    return (f"the last line has {flag['words']} word(s) and is {int(flag['fill'] * 100)}% full ('{flag['last_line']}'); "
            "shorten or lengthen the text (facts only) so that it ends on a fuller line or on one line fewer")


def line_endings_report(results_path) -> str:
    """For workers: `.venv/Scripts/python.exe -c "from app.pdf import line_endings_report as r; print(r('results/x.json'))"`.
    Lists every flagged block of every document of a results file (nothing is written)."""
    import json
    data = json.loads(Path(results_path).read_text(encoding="utf-8"))
    rows: list[str] = []
    for entry in data.get("results", []):
        for kind, content in (entry.get("documents") or {}).items():
            for f in document_line_flags(kind, content):
                rows.append(f"job {entry.get('job_id')} {kind} {f['location']}: {f['words']} word(s), {int(f['fill'] * 100)}% full: "
                            f"'{f['last_line']}'")
    return "\n".join(rows) or "no line-ending problems"


JOB_GAP = 2.6              # mm below the bullets of a job
SKILLS_GAP = 0.6           # mm below the last row of skill groups


def _probe(pdf: _Pdf) -> _Pdf:
    """A second, throw-away document with the same margins and fonts and no automatic page break: a section is drawn
    on it first to learn its height (`_keep_together`). Nothing of it is ever saved."""
    probe = _Pdf("probe", "probe", date(2000, 1, 1))
    probe.set_margins(pdf.l_margin, pdf.t_margin, pdf.r_margin)
    probe.set_auto_page_break(False)
    probe.add_page()
    return probe


def _keep_together(pdf: _Pdf, probe: _Pdf, draw, tail: float = 0.0) -> None:
    """Draw one section with `draw(document)` so that it is not split by a page break.

    The section is drawn on the probe at the same height first; if its height (without the empty `tail` it leaves
    below itself) does not fit on the rest of the page, the real document starts a new page. At the top of a page the
    section is drawn as it is: a section that is longer than a whole page breaks inside, as before (documented limit)."""
    probe.set_y(pdf.get_y())
    start = probe.get_y()
    draw(probe)
    height = probe.get_y() - start - tail
    at_top = pdf.get_y() <= pdf.t_margin + 0.01
    if not at_top and pdf.get_y() + height > pdf.page_break_trigger:
        pdf.add_page()
    draw(pdf)


def _cv_experience(pdf: _Pdf, exp: dict, entry: dict) -> None:
    """One job: title and dates, employer and place (from the fact file), then the bullets of the stored document."""
    title = exp["title"] + (f" – {exp['program']}" if exp.get("program") else "")
    first = _bullet_height(pdf, entry["bullets"][0]["text"]) if entry["bullets"] else 0.0
    if pdf.will_page_break(CV_LINE * 2 + 1.2 + first):    # only a job longer than a page gets here: no stranded title
        pdf.add_page()
    _two_sides(pdf, _clean(title), date_range(exp["start"], exp["end"]), "B", "B")
    _two_sides(pdf, _clean(exp.get("employer") or ""), _clean(exp.get("location") or ""), "", "")
    pdf.ln(1.2)
    for bullet in entry["bullets"]:
        _bullet(pdf, bullet["text"])
    pdf.ln(JOB_GAP)


def _cv_education(pdf: _Pdf, facts: dict, bullet_text: list[str]) -> None:
    """Education, certificates and languages under one heading."""
    _cv_heading(pdf, "EDUCATION & LANGUAGES")
    for edu in facts.get("education", []):
        _two_sides(pdf, _clean(edu["institution"]), date_range(edu["start"], edu["end"]), "B", "B")
        qual = _clean(edu["qualification"]) + (" (not completed)" if edu.get("status") == "not_completed" else "")
        _two_sides(pdf, qual, _clean(edu.get("location") or ""), "", "")
        pdf.ln(1.8)
    for cert in facts.get("certifications", []):
        status = cert.get("status")
        if status == "in_preparation":
            text = f"Preparing for: {_clean(cert['name'])}"
        elif status == "completed":
            text = _clean(cert["name"])
        else:
            continue                                   # an unknown status is never shown as an achievement
        code = re.search(r"\(([^)]+)\)\s*$", cert["name"])
        if code and any(code.group(1) in t for t in bullet_text):
            continue                                   # a bullet of the CV already says it
        pdf.set_font(FAMILY, "B", CV_PT)
        pdf.cell(0, CV_LINE, text, new_x="LMARGIN", new_y="NEXT")
        pdf.note(text)
        pdf.ln(1.8)
    langs = facts.get("languages", [])
    if langs:
        pdf.set_font(FAMILY, "", CV_PT)
        markdown = " | ".join(f"**{_clean(l['language'])}**: {_clean(l['level_cv'])}" for l in langs)
        pdf.multi_cell(0, CV_LINE, markdown, align="J", markdown=True, new_x="LMARGIN", new_y="NEXT")
        pdf.note(*(f"{l['language']}: {l['level_cv']}" for l in langs))


def _cv_skills(pdf: _Pdf, groups: dict, order: list[str], variant: str) -> None:
    """The skill groups, two columns. The lines are lists of short items, not running text, so they stay left-aligned."""
    pdf.ln(1)
    _cv_heading(pdf, "TECHNICAL SKILLS")
    col_w = pdf.epw / 2 - 4
    for i in range(0, len(order), 2):
        cols = []
        for gid in order[i:i + 2]:
            g = groups[gid]
            cols.append((_clean(g["group"]), _wrap_items(_set_font(pdf), [_clean(_item_for(x, variant)) for x in g["items"]], col_w - 7)))
        height = max(CV_LINE * (1.6 + len(lines)) for _, lines in cols)
        if pdf.will_page_break(height):
            pdf.add_page()
        y0 = pdf.get_y()
        for k, (group, items) in enumerate(cols):
            x = pdf.l_margin + k * (pdf.epw / 2 + 2)
            pdf.set_xy(x, y0)
            pdf.set_font(FAMILY, "B", CV_PT)
            pdf.cell(col_w, CV_LINE, group)
            pdf.note(group)
            pdf.set_font(FAMILY, "", CV_PT)
            y = y0 + CV_LINE * 1.5
            for line in items:
                pdf.set_xy(x + 2, y)
                pdf.cell(3.5, CV_LINE, "•")
                pdf.set_xy(x + 6, y)
                pdf.cell(col_w - 6, CV_LINE, line)
                pdf.note(line)
                y += CV_LINE
        pdf.set_xy(pdf.l_margin, y0 + height + SKILLS_GAP)


def _item_for(item: str, variant: str) -> str:
    """The fact file keeps both wordings of an item that differs between the CVs as 'a / b' ('AI prompting / AI prompting
    and workflows'): the first is the Resume wording, the last the Resume CS wording."""
    parts = [p.strip() for p in item.split(" / ")]
    return parts[-1] if variant == "resume_cs" else parts[0]


def _set_font(pdf: _Pdf) -> _Pdf:
    pdf.set_font(FAMILY, "", CV_PT)
    return pdf
