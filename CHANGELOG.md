# Changelog

## v1.0 — first public release

Legal Redact is a local, single-user workstation for reviewing Turkish and English legal
documents for personal data and preparing reviewed DOCX and PDF redactions. v1.0 is the
first version published as a public source repository.

### Read this first

- **Every accuracy figure is measured on synthetic documents.** There is no measurement
  on real legal documents yet. [docs/accuracy.md](docs/accuracy.md) reports the held-out
  synthetic measurement; it does not tell you how the detector performs on a real
  caseload.
- **OCR and scanned-PDF support is experimental and untested** on real scans. The
  automated tests use prepared OCR output and do not run Tesseract.
- **You run it from source.** Clone the repository and run `./start.sh` (Docker), or
  install it with Python as the README describes. There is no prebuilt desktop app, no
  published Docker image, no hosted service and no Word add-in.
- **Detection is not redaction, and redaction is not anonymisation.** Every finding needs
  a human decision, and context can still identify a person after obvious identifiers are
  removed.
- **Single user, local machine only.** Do not expose it to a network. Files and the
  database are unencrypted on local disk. See the threat model in the README and
  [SECURITY.md](SECURITY.md).

### Getting started

- One command, `./start.sh`, builds the Docker image (with local Tesseract and Poppler),
  initialises and migrates the database, indexes six fictional practice documents and
  prints a login URL with a per-install password. Re-running it resumes; `--reset` is the
  explicit destructive option.
- The practice documents are fictional: a Turkish petition, contract, enforcement file
  and notarial notice, an English agreement, and an image-only scanned PDF.

### Detection

All detection is rules-based: regular expressions, check-digit validation and name lists.
There is no machine-learning model in the product, and it makes no outbound network calls.

- **Turkish:** national ID (TCKN) and tax (VKN) numbers with check-digit validation,
  MERSIS and trade registry numbers, bar registrations, courts, chambers, notaries,
  prosecutors' offices, enforcement offices and state directorates (including the city or
  district that names the venue), case, decision and investigation numbers in their
  common citation forms, land-registry parcels, addresses, phone numbers, amounts, dates,
  companies in any letter case, and person names from titles, party roles, signature
  blocks and lists. Keywords match with and without Turkish diacritics.
- **English:** names, addresses, phone numbers, money, dates, courts, claim numbers, sort
  codes and account numbers.
- **Names:** given-name lists widened from official name statistics (sources and licences
  in the README), filtered against ordinary words and place names. Once a person is named
  in full, later bare-surname mentions are proposed under the same placeholder.
- **Legal meaning is kept readable.** Confidentiality, penalty, governing-law and similar
  clauses are not reported as findings; only the identifiers and values inside them are.
  A contractual penalty is not reported as a criminal allegation, a docket reference is
  not reported as an allegation, and contractual confidentiality is not reported as
  attorney–client privilege.
- **Whole-entity spans.** A court finding covers its city, a company finding starts at the
  company name rather than the clause before it, and no finding crosses a line break — so
  an approved finding removes the whole entity from the exported file.
- **Language gate.** The document's language is read from its text. Anything not
  identified as Turkish is reported with residual risk `Unknown` and is not cleared for
  external use; short or name-heavy Turkish documents can be refused the same way.

### Review

- **Matters** group documents. The dashboard lists them with filter chips; the sidebar
  holds only real destinations.
- **Redaction Studio** puts the document at the centre. Click a finding in the text to
  decide it on a card beside it, and move between findings with `J`/`K`.
- **Four decisions:** approve (redact), not sensitive (a false positive), keep unredacted
  (a real finding you deliberately leave in, which keeps release blocked), and add a
  finding the detector missed.
- **Batch review** decides a finding once across every document in a matter. Each
  document is still signed off on its own, and a signed-off document is locked.
- **PDF review** with drawn, moved and resized boxes, each approved or rejected on its
  own. Deciding a finding does not decide its boxes.
- **OCR review** (experimental): OCR text is stored separately and has no effect until a
  reviewer accepts it.

### Export and release gates

- **Reviewed DOCX export** redacts in place across the body, tables, headers, footers,
  footnotes, endnotes, comments, the glossary, tracked changes, field codes and link
  targets, keeping the package structure. It also removes identifying metadata (document
  properties, thumbnail, comment and revision authors, add-in settings, unbound custom XML).
- **Reviewed PDF export** uses true redaction of approved boxes, clears the document
  metadata, removes annotations and every attachment, and redacts approved items from
  outline titles, links and page labels.
- **Independent output scan.** Every reviewed file is scanned for approved items in every
  part before it is handed out. If one remains, the download is refused (HTTP 409) and the
  QA report fails.
- **An approved finding is redacted where it was found**, with other matches of its text
  redacted as a backstop, except inside a finding the reviewer chose to keep.
- **One rendering.** The Studio canvas and the reviewed export resolve through the same
  code, so what a reviewer approves is what is exported. The former lower-assurance
  "preview export" was removed.
- **Release gate for external use.** A document is cleared for use with an external
  hosted model only when extraction is complete, OCR is resolved, the language is covered,
  residual risk is Low, there are no critical findings and no direct identifiers left,
  redaction is complete and a person has approved the review. There is no automatic
  bypass. The UI lists the conditions the server computed and names any that are unmet.

### Security

- Session login with three roles (`admin`, `reviewer`, `viewer`), CSRF tokens on every
  state-changing request, login throttling per user and address, and an audit log that
  never stores document text, placeholder mappings, OCR text or passwords.
- The app refuses to start without a session-signing key, and the Docker setup generates
  one per install.
- Fonts and PDF.js are served locally; the workstation needs no CDN.

### Accuracy tooling

- `accuracy_eval.py` measures the detector on a 40-document synthetic held-out corpus
  (Turkish and English; `.txt`, `.docx` and `.pdf`), scored by character offset, with
  full-span recall reported beside recall. Its figures are published in
  [docs/accuracy.md](docs/accuracy.md), and CI fails when that file disagrees with a fresh
  run.
- `accuracy_audit.py` runs a hand-labelled synthetic regression set. It guards against
  lost capability; it is not an accuracy figure.
- Synthetic phone numbers and email domains in the fixtures and corpus come only from
  reserved or unallocated ranges, and a test enforces it.

### Known limitations

- No real-document accuracy measurement exists.
- OCR and scanned-PDF handling are experimental and untested on real scans.
- English coverage is materially lower than Turkish coverage, and English documents are
  never cleared by the language gate.
- A real Turkish place missing from the bundled place list stays outside a court finding.
- Legacy `.doc` extraction needs macOS `textutil` and is unavailable in the Docker image.
- UDF, legacy DOC, image, XLSX, PPTX and ZIP sources can be reviewed but not exported as
  redacted files; reviewed export is DOCX and PDF only.
- A database indexed by an earlier build may show a stale risk badge until
  `python3 db/backfill_language_coverage.py` is run.

### Licence

MIT for this project's code. PyMuPDF, a dependency you install, is licensed under
AGPL-3.0; see "Licence and third-party components" in the README.
