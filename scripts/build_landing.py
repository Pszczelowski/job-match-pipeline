"""Builds the content of the landing page (goal.md 7.4, v0.101.0). Writes only under web/ (all of it git-ignored).

    python scripts/build_landing.py

web/generated/content.json (allowlisted fields only), and under web/public/generated/: portrait.webp (the profile photo as a
three-tone grey illustration with a transparent background), og.png (1200x630) and cv.pdf (`render_cv(public=True)`, no email, phone, birthdate or address).
Everything is built in a temporary folder first and then checked: the PDF text, link annotations and metadata and content.json must
hold no email address, no phone number and none of the real contact values of profile/master_cv.yaml or config.yaml (the output
names the kind of finding, never the value), and the PDF must show the name and the LinkedIn link. Any finding is exit 1 and
nothing is written to web/.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import documents, pdf                                          # noqa: E402

PUBLIC_CV = ROOT / "profile" / "public_cv.yaml"
LANDING = ROOT / "profile" / "landing.yaml"
PHOTO = ROOT / "profile" / "photo.jpg"
FONT_DIR = ROOT / "app" / "fonts"
CONTENT_FIELDS = ("name", "linkedin", "github", "headline", "tagline", "timeline", "skills", "languages", "project")
TIMELINE_FIELDS = ("title", "employer", "city", "dates", "bullets")

PALETTE = ((0x1B, 0x1B, 0x1B), (0xB6, 0x3E, 0x96), (0x9A, 0x9A, 0x9A), (0xF5, 0xF5, 0xF5))      # dark, magenta, grey, light (og.png)
PORTRAIT_SIZE = (960, 1200)
PORTRAIT_TONES = ((0x1B, 0x1B, 0x1B), (0x6B, 0x6B, 0x6B), (0xC8, 0xC8, 0xC8))      # the three bands of the portrait (no pink on the face)
PORTRAIT_CUTS = (80, 150)             # grey thresholds between the three bands (after autocontrast)
PORTRAIT_MEDIAN = 9                   # median filter size before the bands
PORTRAIT_BAND_BLUR = 2.5              # blur of the band map, against speckle
BG_TOLERANCE = 18                     # grey levels below the background that still count as background
BG_SHARE = (0.15, 0.85)               # the subject must fill this share of the crop, else the background removal is not trusted
OG_SIZE = (1200, 630)

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"(?<![\w.])\+?\d[\d ()/.\-]{7,}\d")
MIN_PHONE_DIGITS = 8


class BuildError(Exception):
    pass


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ----------------------------------------------------------------------------- content

def _lookups(master: dict) -> tuple[dict, dict]:
    experiences = {e["id"]: e for e in master.get("experience", [])}
    bullets = {b["id"]: b for e in master.get("experience", []) for b in e["bullets"]}
    return experiences, bullets


def _bullet_text(bullets: dict, bullet_id: str, wording: str) -> str:
    bullet = bullets.get(bullet_id)
    if bullet is None:
        raise BuildError(f"bullet {bullet_id} is not in master_cv.yaml")
    text = (bullet.get("texts") or {}).get(wording)
    if not text:
        raise BuildError(f"bullet {bullet_id} has no `{wording}` wording")
    return text


def check_ids(master: dict, public_cv: dict, landing: dict) -> None:
    """Every experience, bullet, skill and language named in the two files exists in master_cv.yaml."""
    experiences, bullets = _lookups(master)
    items = {item for g in master.get("skills", []) for item in g["items"]}
    groups = {g["id"] for g in master.get("skills", [])}
    for exp_id, entries in (public_cv.get("experience") or {}).items():
        if exp_id not in experiences:
            raise BuildError(f"experience {exp_id} (public_cv.yaml) is not in master_cv.yaml")
        for entry in entries:
            _bullet_text(bullets, entry["id"], entry["from"])
    for gid in public_cv.get("skills") or []:
        if gid not in groups:
            raise BuildError(f"skill group {gid} (public_cv.yaml) is not in master_cv.yaml")
    for entry in landing.get("timeline") or []:
        if entry["experience"] not in experiences:
            raise BuildError(f"experience {entry['experience']} (landing.yaml) is not in master_cv.yaml")
        for bid in entry["bullets"]:
            if bid not in bullets:
                raise BuildError(f"bullet {bid} (landing.yaml) is not in master_cv.yaml")
    for item in landing.get("skills") or []:
        if item not in items:
            raise BuildError(f"skill item {item!r} (landing.yaml) is not in master_cv.yaml")
    languages = {l["id"] for l in master.get("languages", [])}
    unknown = set(landing.get("languages") or []) - languages
    if unknown:
        raise BuildError(f"language {sorted(unknown)} (landing.yaml) is not in master_cv.yaml")


def _wording(public_cv: dict, bullet_id: str) -> str:
    """The wording (resume or resume_cs) the public CV uses for a bullet; `resume` when the CV does not carry it."""
    for entries in (public_cv.get("experience") or {}).values():
        for entry in entries:
            if entry["id"] == bullet_id:
                return entry["from"]
    return "resume"


def build_content(master: dict, public_cv: dict, landing: dict) -> dict:
    """content.json: allowlisted fields only (never email, phone, birthdate or address)."""
    check_ids(master, public_cv, landing)
    experiences, bullets = _lookups(master)
    person = master["person"]
    timeline = []
    for entry in landing["timeline"]:
        exp = experiences[entry["experience"]]
        texts = []
        for bid in entry["bullets"]:
            have = bullets[bid].get("texts") or {}
            wording = _wording(public_cv, bid)
            if not have.get(wording):
                wording = next((k for k in ("resume", "resume_cs") if have.get(k)), wording)
            texts.append(_bullet_text(bullets, bid, wording))
        timeline.append({"title": entry.get("title") or exp["title"], "employer": exp.get("employer") or "",
                         "city": exp.get("location") or "", "dates": pdf.date_range(exp["start"], exp["end"]), "bullets": texts})
    wanted = landing.get("languages")
    languages = [{"language": l["language"], "level": l["level_cv"]} for l in master.get("languages", [])
                 if not wanted or l["id"] in wanted]
    return {"name": person["name"], "linkedin": person["linkedin"], "github": public_cv["github"],
            "headline": landing["headline"], "tagline": landing["tagline"], "timeline": timeline,
            "skills": list(landing["skills"]), "languages": languages, "project": landing["project"]}


def build_cv_doc(master: dict, variants: dict, public_cv: dict) -> dict:
    """The stored-`cv` shaped document of the public CV (texts verbatim from master_cv.yaml)."""
    _experiences, bullets = _lookups(master)
    experience = []
    for exp in master["experience"]:
        entries = (public_cv.get("experience") or {}).get(exp["id"])
        if entries:
            experience.append({"experience_id": exp["id"], "bullets": [
                {"fact_id": e["id"], "text": _bullet_text(bullets, e["id"], e["from"])} for e in entries]})
    summary = " ".join(variants["variants"][public_cv["summary_from"]]["summary"].split())
    return {"variant": public_cv["variant"], "summary": summary, "experience": experience,
            "skills": list(public_cv.get("skills") or [])}


def render_public_cv(master: dict, variants: dict, public_cv: dict, out_path: Path, photo: Path = PHOTO) -> None:
    facts = dict(master)
    facts["person"] = dict(master["person"], github=public_cv["github"])
    photo_arg = photo if public_cv.get("show_photo") else None
    res = pdf.render_cv(build_cv_doc(master, variants, public_cv), facts, variants, out_path, photo_arg, public=True)
    if res.warnings:
        raise BuildError("the CV rendered with warnings: " + "; ".join(res.warnings))


# ----------------------------------------------------------------------------- illustrations

def _background(grey: Image.Image) -> Image.Image | None:
    """Mask (255 = subject) of a light, near-uniform background: pixels near the background grey that connect to the top, left or
    right edge (the bottom edge holds the clothes). None when the result is not plausible."""
    w, h = grey.size
    ref = sorted(grey.getpixel((x, 0)) for x in range(w))[w // 2]
    cand = grey.point([255 if v >= ref - BG_TOLERANCE and ref > 150 else 0 for v in range(256)])
    for x in range(w):
        if cand.getpixel((x, 0)) == 255:
            ImageDraw.floodfill(cand, (x, 0), 128)
    for y in range(h - 1):
        for x in (0, w - 1):
            if cand.getpixel((x, y)) == 255:
                ImageDraw.floodfill(cand, (x, y), 128)
    mask = cand.point([0 if v == 128 else 255 for v in range(256)])
    share = sum(mask.histogram()[255:]) / (w * h)
    return mask if BG_SHARE[0] <= share <= BG_SHARE[1] else None


def illustration(photo: Path, box) -> Image.Image:
    """The portrait: RGBA, the subject in three grey bands (#1B1B1B, #6B6B6B, #C8C8C8), the light background transparent (a soft
    circular mask when it cannot be told apart). EXIF turn, crop, LANCZOS upscale, median blur, bands, blurred band map. A fresh image
    (no EXIF, ICC or other metadata of the photo)."""
    with Image.open(photo) as src:
        img = ImageOps.exif_transpose(src).convert("RGB").crop(tuple(box))
    mask = _background(ImageOps.grayscale(img))
    img = img.resize(PORTRAIT_SIZE, Image.LANCZOS)
    grey = ImageOps.autocontrast(ImageOps.grayscale(img), cutoff=1).filter(ImageFilter.MedianFilter(PORTRAIT_MEDIAN))
    levels = grey.point([0 if v < PORTRAIT_CUTS[0] else 128 if v < PORTRAIT_CUTS[1] else 255 for v in range(256)])
    levels = levels.filter(ImageFilter.GaussianBlur(PORTRAIT_BAND_BLUR))
    tone = levels.point([0 if v < 64 else 1 if v < 192 else 2 for v in range(256)])
    channels = [tone.point([PORTRAIT_TONES[t][c] if t < 3 else 0 for t in range(256)]) for c in range(3)]
    if mask is None:
        mask = Image.new("L", PORTRAIT_SIZE, 0)
        ImageDraw.Draw(mask).ellipse((0, 0, PORTRAIT_SIZE[0] - 1, PORTRAIT_SIZE[1] - 1), fill=255)
        alpha = mask.filter(ImageFilter.GaussianBlur(2))
    else:
        mask = mask.resize(PORTRAIT_SIZE, Image.BICUBIC).filter(ImageFilter.GaussianBlur(6))
        alpha = mask.point([0 if v < 128 else 255 for v in range(256)]).filter(ImageFilter.GaussianBlur(1.2))
    out = Image.merge("RGBA", (*channels, alpha))
    return out


def og_image(name: str, headline: str) -> Image.Image:
    img = Image.new("RGB", OG_SIZE, PALETTE[3])
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, 24, OG_SIZE[1]), fill=PALETTE[1])
    big = ImageFont.truetype(str(FONT_DIR / "EBGaramond-Bold.ttf"), 96)
    small = ImageFont.truetype(str(FONT_DIR / "EBGaramond-Regular.ttf"), 44)
    draw.text((90, 210), name, font=big, fill=PALETTE[0])
    draw.rectangle((90, 340, 330, 346), fill=PALETTE[1])
    draw.text((90, 380), headline, font=small, fill=PALETTE[2])
    return img


# ----------------------------------------------------------------------------- the contact check

def real_contact_values(master: dict, config_path: Path | None = ROOT / "config.yaml") -> dict[str, str]:
    """kind -> real value (email, phone, birthdate, address ...). Used for comparison only; never printed."""
    person = master["person"]
    values = {"email": person.get("email"), "phone": person.get("phone"), "birthdate": person.get("birthdate"),
              "address": person.get("address")}
    if config_path is not None and Path(config_path).is_file():
        cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        for section, body in cfg.items():
            if isinstance(body, dict):
                for key, value in body.items():
                    if isinstance(value, str) and re.search(r"street|home|address", key, re.IGNORECASE):
                        values[f"config {section}.{key}"] = value
    return {k: str(v) for k, v in values.items() if v}


def _norm(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def pdf_texts(path: Path) -> dict[str, str]:
    """Text, link targets and metadata of a PDF, by source name."""
    reader = PdfReader(str(path))
    text = " ".join(" ".join((p.extract_text() or "").split()) for p in reader.pages)
    uris = []
    for page in reader.pages:
        for annot in page.get("/Annots") or []:
            action = annot.get_object().get("/A") or {}
            uris.append(str(action.get("/URI", "")))
    meta = " ".join(str(v) for k, v in (reader.metadata or {}).items() if k not in ("/CreationDate", "/ModDate"))      # dates look like numbers
    return {"pdf text": text, "pdf links": " ".join(uris), "pdf metadata": meta}


def contact_findings(texts: dict[str, str], values: dict[str, str], name: str, linkedin: str, expect_pdf: bool = True,
                     patterns: bool = True) -> list[str]:
    """Kinds of finding (never values). `texts`: source name -> text. The name and the LinkedIn link must stand in the PDF.
    `patterns=False` looks for the real values only (no e-mail or phone shapes)."""
    findings: list[str] = []
    for source, text in texts.items():
        if patterns and (EMAIL_RE.search(text) or "mailto:" in text.lower()):
            findings.append(f"email address or mailto link in {source}")
        if patterns and any(len(_digits(m.group())) >= MIN_PHONE_DIGITS for m in PHONE_RE.finditer(text)):
            findings.append(f"phone-like number in {source}")
        for kind, value in values.items():
            if kind == "birthdate":
                digits = _digits(value)                               # 19920905 from an ISO date
                variants = {value, f"{digits[6:8]}.{digits[4:6]}.{digits[:4]}"} if len(digits) == 8 else {value}
                hit = any(v in text for v in variants)
            elif kind == "phone":
                hit = len(_digits(value)) >= MIN_PHONE_DIGITS and _digits(value) in _digits(text)
            else:
                needle = _norm(value)
                hit = len(needle) >= 5 and needle in _norm(text)
            if hit:
                findings.append(f"the real {kind} in {source}")
    if expect_pdf:
        if name not in texts.get("pdf text", ""):
            findings.append("the name is missing from the pdf text")
        if linkedin not in texts.get("pdf text", "") and linkedin not in texts.get("pdf links", ""):
            findings.append("the LinkedIn link is missing from the pdf")
    return findings


# ----------------------------------------------------------------------------- the build

def build(web: Path, master: dict, variants: dict, public_cv: dict, landing: dict, photo: Path = PHOTO,
          config_path: Path | None = ROOT / "config.yaml") -> list[str]:
    """Builds into a temporary folder, checks it, then moves it under `web`. Returns the files written (relative to `web`);
    raises BuildError on a finding or an unknown id (nothing is written to `web`)."""
    content = build_content(master, public_cv, landing)
    with tempfile.TemporaryDirectory(prefix="landing-") as tmp_name:
        tmp = Path(tmp_name)
        (tmp / "generated").mkdir()
        gen = tmp / "public" / "generated"
        gen.mkdir(parents=True)
        (tmp / "generated" / "content.json").write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        illustration(photo, landing["portrait_box"]).save(gen / "portrait.webp", "WEBP", lossless=True, exact=True)
        og_image(content["name"], content["headline"]).save(gen / "og.png", "PNG")
        render_public_cv(master, variants, public_cv, gen / "cv.pdf", photo)
        texts = pdf_texts(gen / "cv.pdf")
        texts["content.json"] = (tmp / "generated" / "content.json").read_text(encoding="utf-8")
        findings = contact_findings(texts, real_contact_values(master, config_path), master["person"]["name"],
                                    master["person"]["linkedin"])
        if findings:
            raise BuildError("contact check failed: " + "; ".join(sorted(set(findings))))
        written = []
        for rel in ("generated", "public/generated"):
            target = web / rel
            target.mkdir(parents=True, exist_ok=True)
            for old in target.iterdir():              # the folder itself stays (a shell or server may sit in it)
                shutil.rmtree(old) if old.is_dir() else old.unlink()
            shutil.copytree(tmp / rel, target, dirs_exist_ok=True)
            written += [f"{rel}/{p.name}" for p in sorted(target.iterdir())]
        return written


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0]).parse_args(argv)
    master, variants = documents.load_profile()
    try:
        written = build(ROOT / "web", master, variants, load_yaml(PUBLIC_CV), load_yaml(LANDING))
    except BuildError as exc:
        print(f"build_landing: {exc}", file=sys.stderr)
        return 1
    print("build_landing: wrote " + ", ".join(written))
    return 0


if __name__ == "__main__":
    sys.exit(main())
