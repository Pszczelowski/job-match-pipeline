# Bundled fonts: EB Garamond

Used by `app/pdf.py` for the generated cover letters and CVs (goal.md 5.6). EB Garamond replaces the Garamond of the
user's Word samples, which is not freely licensed.

- Files: `EBGaramond-Regular.ttf`, `EBGaramond-Bold.ttf`, `EBGaramond-Italic.ttf`, `EBGaramond-BoldItalic.ttf`
  (static TrueType, fonts version 1.001 to 1.002, as built by the project).
- Source: https://github.com/octaviopardo/EBGaramond12 , folder `fonts/ttf/`, commit 6d9aff51f8d0 of the file history
  (downloaded 1 Nov 2026).
- Licence: SIL Open Font License 1.1, text in `OFL.txt` (copied from
  https://github.com/google/fonts/tree/main/ofl/ebgaramond , it matches the copyright line inside the font files:
  "Copyright 2017 The EB Garamond Project Authors"). The fonts are used unmodified. The licence allows bundling and
  embedding in documents.
- Google Fonts offers EB Garamond only as variable fonts; fpdf2 needs static files, hence the project's own build.
