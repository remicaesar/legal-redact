# AGENTS.md

This file provides guidance to Codex when working with code in this repository.

## What this is

A cautious legal-tech prototype (Flask + SQLite) for classifying Turkish/English legal documents and preparing privacy-reviewed, risk-reduced outputs for LLM workflows. It deliberately avoids claiming outputs are "anonymous" unless re-identification risk has been assessed as genuinely low — see `legal_analyzer/taxonomy.py` (`TERMINOLOGY`, `OUTPUT_POSITIONING`) for the redaction/pseudonymization/de-identification/anonymization distinctions used throughout the UI, API, and code comments.

## Non-negotiable engineering constraints

- Do not weaken privacy, redaction, OCR, or export safety gates.
- Do not bypass human review or approval requirements.
- Detection is not equivalent to completed redaction.
- Do not expose pseudonym mappings in exports, logs, or frontend responses.
- Do not place raw sensitive document text in audit metadata.
- Preserve role-based permissions and authentication.
- Preserve fail-closed behavior.
- Preserve existing backend route contracts unless the user explicitly approves changes.
- Do not send document contents to external services.
- Use only synthetic documents and synthetic personal data in tests.
- Do not delete functionality merely because it is difficult to redesign.
- Avoid introducing a new frontend framework unless the existing architecture clearly cannot support the required interface.

## Frontend direction

- Create a premium, restrained legal-tech interface.
- Organize the experience around matters rather than database records.
- Make the document the center of the review workspace, with contextual AI assistance beside it.
- Use progressive disclosure for technical information.
- Support accessible, keyboard-driven review.
- Present one obvious primary action per screen.
- Use a warm off-white background, white surfaces, near-black text, and a restrained oxblood accent (`--color-accent`, #6e2a32).
- Avoid generic SaaS dashboard patterns, glassmorphism, excessive gradients, giant KPI cards, robot imagery, magic-wand icons, and decorative AI effects.

### The sidebar lists places; views are chips

A nav item must be a destination. Three of them once pointed at `/?review_status=…`, `/?ocr_status=…`
and `/?file_type=.docx` — the same dashboard with a querystring — and the sidebar went on
highlighting "Dashboard" while you stood on one. Those are chips above the document table now.

The dashboard still reads **every** `/api/documents` filter key off its own querystring
(`initializeFiltersFromUrl` in `templates/index.html`), so all the old rail URLs remain valid
bookmarks; a key with no chip renders as a named "Filtered by" pill rather than silently shortening
the list. `tests/test_dashboard_navigation.py` pins both halves — no nav href contains `?`, and each
retired URL still returns the right document ids. Do not add a nav entry with a querystring, and do
not drop a filter key from that JS object.

The shared chrome lives in `templates/base.html`, which every page extends.

### /welcome is built, not templated

`templates/landing.html` and `static/landing/` are build output of `landing/` (React + Vite + visx): prerendered, so the page reads without JavaScript, then hydrated. It is the one React page, on purpose; the workstation stays framework-free, and Node is needed only to rebuild (`cd landing && npm ci && npm run build`), never to run the app. Edit `landing/src/` or `landing/landing.template.html`, never the generated files. The demo's findings, placeholders and gate states come from running the app (`.venv/bin/python landing/scripts/gen_demo.py`), and `tests/test_landing.py` fails when a detection or gate change moves them: regenerate, rebuild, commit all of it. `landing/src/plan.js` ports `redaction_plan.plan_segments` for that one document; `node landing/scripts/verify_plan.mjs` (run in CI) checks it against the product's own output. The page never computes the release gate; it only shows states the app computed. See `landing/README.md`.

## Required working method

Before changing code, inspect the relevant routes, templates, styles, JavaScript, tests, and data flow. Keep changes focused, preserve existing behavior unless a change is explicitly approved, run tests appropriate to the modified surface, and report exactly what changed.

## Setup and running

For the Docker onboarding path, use `./start.sh`; see `README.md` for prerequisites,
persistent volumes, credentials, and the destructive `--reset` option. Setup lives
in `docker/bootstrap.py`; `samples/README.md` explains the fictional practice files.
Run `python -m unittest tests.test_onboarding` when changing that surface.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export LEGAL_ANALYZER_ADMIN_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
python3 db/init_db.py       # safe: keeps an existing db/legal_documents.db and tops it up
python3 db/init_db.py --reset  # DESTRUCTIVE: prints row counts, then wipes and reseeds
python3 db/migrate.py       # apply pending migrations to an existing db
python3 classify.py         # scans DEFAULT_STOCK_DIR + extra files, populates the db
python3 app.py
```

App runs at `http://127.0.0.1:5000`. Set `LEGAL_ANALYZER_ADMIN_PASSWORD` and `LEGAL_ANALYZER_SECRET_KEY` in your environment; neither has a default and the app refuses to start without the secret key. The first local admin user is created from `LEGAL_ANALYZER_ADMIN_USERNAME` (default `admin`) / `LEGAL_ANALYZER_ADMIN_PASSWORD` env vars on first DB connection (`bootstrap_admin_from_env` in `app.py`).


### Database migrations

Migrations are plain `.sql` files in `db/migrations/`, applied in filename-sort order and tracked in a `schema_migrations` table (`db/migrate.py`). `ensure_db_ready()` in `app.py` calls `apply_migrations` automatically on first DB connection per process, so the app self-migrates — but run `python3 db/migrate.py --status` to check state, and `python3 db/migrate.py` to apply manually (e.g. after pulling new migrations before running tests). To add a migration, add a new numbered `.sql` file to `db/migrations/`; don't edit `db/schema.sql` retroactively for existing databases (it's only used by `db/init_db.py` for fresh DBs).

Two constraints make column *removal* impractical here, so prefer retiring a column in place (annotate it in `db/schema.sql`, delete every read, pin that with a test):

- **A migration may not reference any `documents` column beyond `id`/`filename`/`filepath`.** `tests/test_migrations.py::test_existing_database_upgrades_without_data_loss` applies the whole chain to a three-column legacy table, so anything else — even in a `WHERE` clause or an index — fails the suite.
- **`db/schema.sql` must stay the state a database actually ends up in.** `tests/test_csrf.py`, `test_app_workflow.py`, `test_gate_reachability.py` and `test_landing.py` build their database from it *without* running migrations, so a column dropped by a migration but still listed there (or vice versa) makes every test database disagree with every real one.

SQLite has no `DROP COLUMN IF EXISTS`, and the only idempotent alternative is a twelve-step rebuild of `documents` — the table the FTS5 content-table, its three triggers, nine indexes and every foreign key hang off.

## Tests

Tests use `unittest`, not pytest (no pytest in `requirements.txt`). Run files as modules:

```bash
python3 -m unittest tests.test_app_workflow
python3 -m unittest tests.test_app_workflow.AppWorkflowTests.test_some_method   # single test
python3 -m unittest discover -s tests -t .
```

Synthetic phone numbers and email domains in fixtures, tests and `eval/` come only from reserved or unallocated ranges (Ofcom drama numbers, the unallocated Turkish `052x` mobile block, RFC 2606 `example.*` domains) — the table and how each was checked is "Reserved contact ranges" in `eval/README.md`, and `tests/test_reserved_contacts.py` enforces it.

Tests generally build fixtures in-memory or under a `TemporaryDirectory` and construct a fresh SQLite DB per test (see `tests/test_app_workflow.py`) rather than touching `db/legal_documents.db`.

The `-t .` on the discover command is load-bearing. It makes `tests` import as a package, which runs `tests/__init__.py` — that file points `app.test_client_class` at a client which carries the session CSRF token. Without it the package is skipped, every state-changing request in the suite arrives with no token, and ~58 tests fail with 403. CSRF protection is deliberately *not* disabled under test: `tests/test_csrf.py` uses a raw `FlaskClient` to prove the check still rejects. Anything that calls `reload(app_module)` must re-run `install_csrf_client()` afterwards, since the reload builds a new Flask object with the default client (see `reload_app()` in `tests/test_app_hardening.py`).

## Accuracy and benchmark tooling

These are not unit tests but repo-specific validation scripts — run them after touching `legal_analyzer/privacy.py` detection rules or extraction/OCR logic:

### Two accuracy tools, and they are not interchangeable

`gold/` + `accuracy_audit.py` is the **regression ratchet**. Its labels and the rules were edited
in the same commits, so its recall is 1.000 in every category and it cannot produce a false
negative. CI gates on `false_negative_count == 0`, which is exactly the guarantee it can give: no
capability is silently lost. It is not an accuracy figure and quoting it as one is the mistake
`eval/` exists to correct.

`eval/` + `accuracy_eval.py` is the **measurement**: 40 generated TR/EN documents, 492 entity
spans with generator-emitted offsets, scored per category, per language and per format
(`.txt`/`.docx`/`.pdf`). Nothing here may be used to tune a rule — see `eval/README.md`, which
states that rule and why breaking it costs both corpora. Its numbers are reported, never gated at
a floor; `docs/accuracy.md` is the committed, citable copy and CI fails when it disagrees with a
fresh run, so a detection change cannot merge without the numbers it moved.

Three scoring differences from the audit, each of which was hiding something: matching is by
character **offset overlap**, not substring (a right-category finding at the wrong place is a miss
*and* a false positive); **full-span recall** is reported beside recall, because redaction removes
only what a finding covers; and an entity that did not survive text extraction is counted as
`unlocatable` rather than as a detection miss, with a `wrapped`/`interleaved`/`absent` cause.

```bash
python3 eval/generate_corpus.py --check    # the eval corpus is what its generator says
python3 accuracy_eval.py                   # the held-out measurement -> eval_report.json
python3 accuracy_eval.py --write-docs      # refresh docs/accuracy.md after a detection change
python3 -m unittest tests.test_accuracy_eval
```

```bash
python3 accuracy_audit.py --labels gold/gold_labels.example.json   # the ratchet; watch false_low_count == 0 and risk_shortfall_count == 0
python3 benchmark.py                                               # full benchmark -> benchmark_report.json
python3 benchmark.py --limit 25 --api-iterations 5                 # fast smoke benchmark
python3 benchmark.py --stock-dir tests/fixtures --api-iterations 5  # measures the detection pipeline on synthetic data only
python3 benchmark.py --api-iterations 1000 --api-concurrency 50    # local concurrency check (still not real load testing)
```

`benchmark.py` imports `app`, so it exits immediately unless `LEGAL_ANALYZER_SECRET_KEY`
is set — it refuses to invent a session-signing key. Its `pipeline` arm also measures
whatever `--stock-dir` points at, defaulting to `DEFAULT_STOCK_DIR` (`documents/`); when
that directory is absent the run still succeeds and reports `documents: 0`, so a green
benchmark proves nothing about extraction/classification/privacy timing. Point it at
`tests/fixtures` for a run that exercises the pipeline without touching real documents.

`gold/` holds the synthetic gold-label set (37 documents, 365 required labels: 34 `.txt` fixtures, 23 Turkish, 6 English under `tests/fixtures/english/` and 5 name documents under `tests/fixtures/names/`, and the 3 Turkish `.docx` practice samples). Extend it with real labeled documents before trusting it for production accuracy claims — and extend it, rather than `eval/`, when a detection change needs a regression case.

## Architecture

**`app.py`** (~1,660 lines) is the shared core: the Flask app object and its config, DB access, session-based auth/RBAC (`admin`/`reviewer`/`viewer`), audit logging, document persistence helpers, and the release-gate refresh. HTTP routes live in `blueprints/` and import these helpers. It does not contain detection/extraction/redaction logic itself — it calls into `legal_analyzer/*` and `classify.py`.

**`legal_analyzer/`** — the core domain logic, split by concern:
- `taxonomy.py` — category/subcategory vocabulary, supported file extensions, terminology definitions (redaction vs. pseudonymization vs. de-identification vs. anonymization). Other modules import from here rather than hardcoding categories.
- `classifier.py` — document classification (category/subcategory), title/date/language/version detection.
- `extraction.py` — text extraction across DOCX, PDF, UDF, TXT/MD, PPTX, XLSX, legacy DOC (via macOS `textutil`), and ZIP containers (nested ZIPs skipped). Marks extraction `Complete`/`Partial`/`Failed`, degrading status when the size cap truncates output.
- `privacy.py` — the PII/privacy-risk detection engine: `analyze_privacy()`, `refresh_release_state()`, and the external-LLM release gate logic (`EXTERNAL_LLM_BLOCKED`/`EXTERNAL_LLM_ALLOWED`). The rules it runs live in `privacy_*` modules split by concern, and `privacy.py` re-exports every name from them: `privacy_rules.py` (`DETECTION_RULES` — regex-based, Turkish-aware, e.g. court/notary/case-number patterns, dotted-İ handling), `privacy_courts.py` (court/authority rules, `_COURT_PLACE_PREFIX`, `_venue_prefix_start`, `_generic_court_matches`, `_assign_grouped_placeholders`), `privacy_names.py` (people, roles, companies, `_NAME_SEP`, `_venue_surname_mentions`), `privacy_identifiers.py` (phones, dates, amounts, case numbers, accounts, addresses, TCKN/VKN check-digit validation), `privacy_context.py` (health/criminal/privilege sweeps, language-evidence spans) and `privacy_common.py` (`DIRECT_IDENTIFIER_CATEGORIES`, the rule record, finding constructors). This is the most safety-critical module — see "Safety gates" below before changing it.
- `ocr.py` — local-first OCR (`tesseract` + `pdftoppm` via `brew install tesseract tesseract-lang poppler`). OCR text is stored separately from extracted text and never affects findings/redaction/release-readiness until a reviewer explicitly accepts it (`ocr/accept` endpoint). Tesseract TSV output captures word bounding boxes (`ocr_tokens`) used to map accepted OCR findings to PDF coordinate boxes.
- `docx_redactor.py` / `docx_quality.py` — actual reviewed DOCX redaction (in-place text redaction across body/tables/headers/footers/footnotes/endnotes/comments, preserving package structure) and its QA checks (package parts, XML node/paragraph/table counts, approved-target leakage).
- `redaction_plan.py` — the ONE post-decision rendering. The browser review canvas and the reviewed DOCX export must both resolve through it (it calls `docx_redactor.resolve_redaction`), or they drift apart and a lawyer approves a rendering that is not the deliverable. Do not add a third. (There were three: the "Preview TXT / Preview DOCX (lower assurance)" export was deleted — a rebuilt `<name>_redacted.txt` headed `PRIVACY-REVIEWED REDACTED EXPORT` whose pre-decision body was the unredacted source.) `privacy.build_redacted_preview` is NOT a rendering — it is the detection-time measurement stored on the profile, like `residual_risk`.
- `export_scan.py` — the independent output scan: searches the built DOCX/PDF bytes everywhere (not where the redactor edits) for approved findings; `build_reviewed_docx`/`build_reviewed_pdf` refuse with a 409 on any leak and both QA reports fail on it. It must not adopt the redactor's model of where data lives.
- `pdf_redactor.py` — actual reviewed PDF redaction via PyMuPDF, driven by reviewer-approved coordinate regions (not the browser preview overlay, which is UI-only). Includes PDF export QA (extractable-text leak checks attributed to specific `region_id`s, metadata scrubbing, annotation removal).

**`classify.py`** — the standalone indexing script: scans a document root (`DEFAULT_STOCK_DIR`) plus extra files, extracts/classifies/privacy-analyzes each, and populates the `documents` table. `detect_client()` matches filenames/text against known client aliases in the `clients` table.

**`db/`** — SQLite schema (`schema.sql`, used only for fresh init) + forward-only migrations (`migrations/*.sql`, tracked via `schema_migrations`). Key tables: `documents`, `clients`, `matters`, plus findings, audit_log, OCR pages/tokens, and PDF redaction regions (see schema for exact columns). `data/exports/document_<id>/` stores saved reviewed export artifacts, referenced by the artifact timeline.

**`templates/`** + **`static/`** — server-rendered Jinja UI. `studio.html` is the large Redaction Studio workspace (PDF viewer + region drawing, finding review, OCR review). The in-browser PDF viewer is a vendored PDF.js build under `static/vendor/pdfjs/` — no CDN dependency.

`studio.html` has ONE finding-review renderer, `renderReviewList()`, for every file format. There were four: `renderReviewGroups` / `renderBulkReviewTable` / the guided card were three complete panes mounted at once and merely `hidden` (82% of the page's DOM), and `renderReviewQueue()` replaced the list for PDFs and retitled the panel. They differed in *capability*, not layout — the default cost 29 clicks on a 29-finding document, By-type cost 14, and the bulk table could not finish at all because its only approve button covered LOW and MEDIUM. Do not add a second renderer, a mode switcher, or a format fork; `tests/test_review_pane.py` fails if any comes back. A single finding is decided on its card (`openFindingPopover`), opened from its bracket or PDF box; the review column carries only the whole-document batch and the findings the canvas cannot show, and a PDF finding's boxes sit on the same card — but **deciding a finding must not decide its boxes**: `pdf_export_blockers()` refuses on unreviewed boxes and that safeguard is the point of the separate step (pinned by `BoxReviewStaysItsOwnStepTests`).

### Safety gates (do not weaken without explicit instruction)

External hosted LLM use is gated on ALL of: extraction complete, OCR resolved, document language covered by the detection rules, residual risk Low, zero critical findings, no direct identifiers remaining, redaction completed, and human review approved. Human approval has no substitute: the `auto_mode_enabled` arm that once stood in for it was removed, `documents.auto_mode_enabled` is a retired column no code reads, and `tests/test_auto_mode_removed.py` fails if any read comes back. Any extraction warning (`Partial`/`Failed`) blocks external LLM use until OCR/manual review resolves it. Detected findings are not the same as successfully redacted findings — only findings with `approved` or `added_by_reviewer` status get redacted on export. If OCR text is accepted without coordinate tokens, native PDF export fails closed until manual boxes are drawn and approved. Raw sensitive text, original-to-placeholder mappings, OCR text, passwords, and replacement text must never be written to `audit_log` metadata or artifact-timeline metadata.

Each gate states its conditions exactly once, as data, and the UI renders that list rather than rebuilding it. `external_llm_gate_policy()` returns `conditions` (each with `passed`; `/welcome` quotes the count and the labels from `landing/src/demo.json`, and `tests/test_landing.py` fails if either drifts) and derives `failed_conditions` and `policy` from it; `docx_export_conditions()` / `pdf_export_conditions()` in `app.py` are what `get_exportable_docx()` and `pdf_export_blockers()` refuse on, and `export_gate_state()` exposes the same list as `export_gate` on `/api/document/<id>`. Never re-derive a gate in the browser: a JavaScript copy that modelled 5 of the 7 release conditions once put five green ticks and a live download button directly above the gate's own red "Blocked". `tests/test_trust_surfaces.py` pins both halves — that the studio's release panel hardcodes no condition of its own, and that the "This export" panel and the export endpoint agree across a matrix of review states.

The gate clears only Turkish, and says so rather than clearing what it cannot read. The calibration history, measurements, reverted attempts and known limitations behind every rule below are in `docs/language-gate.md` — read it before changing any of this.

* `classifier.detect_language` derives TR / EN / Unknown from the extracted **text**, never the filename; `privacy.language_coverage()` turns that into the gate's `language_supported` condition. Anything not TR (Unknown included) gets residual risk `Unknown`, `human_review_required` True and a refused gate. Silence — a short, empty or unidentifiable extraction, or a stored profile with no `language_coverage` key — fails toward refusal. Only the exact code `detect_language` emits counts (`'tr'` is not Turkish here).
* Turkish is claimed only on Turkish marker **words** that fall **outside** every detected entity span, never on letters, and only when English is not clustered: the measure is the longest English-marker run (Turkish evidence resets it; markers more than `_ENGLISH_RUN_GAP` words apart do not chain), capped at `_MAX_ENGLISH_RUN`. It measures position, not quantity — do not replace it with a share or a count. **A proper noun is not a language**: never reintroduce a letter-based path, a gazetteer/TCKN top-up, or any rule keyed on the *absence* of English function words.
* `_marker_evidence(text, entity_spans)` takes the spans; callers pass them via `privacy.language_evidence_spans` (`classifier.py` must never import `privacy.py` — cycle). Omitting spans is the permissive direction, so a caller that cannot supply them must not clear (the backfill leaves a profile with no `risk_map` unanswered). The span exclusion guards the **Turkish arm only**; English markers count wherever they fall.
* The two marker lists must stay disjoint from the other language's ordinary words (`OrdinaryTurkishIsNeverEnglishEvidenceTests`).
* The constants in `legal_analyzer/classifier.py` — `_MIN_WORDS` = 20, `_MIN_SIGNAL` = 3, `_ENGLISH_RUN_GAP` = 4, `_MAX_ENGLISH_RUN` = 5 — are measured. Do not lower the first two to rescue a refused Turkish document, and do not retune the last two to reach field-shaped English annexes. There is no headroom (`test_this_calibration_has_no_headroom`); five gold fixtures are deliberately refused (`test_every_turkish_gold_fixture_is_identified_as_turkish`). Known gaps — a field-shaped English annex now gets findings for its names, address and phone yet still reads as Turkish, and the refusals of ordinary short/entity-heavy Turkish are unchanged by English detection; neither is fixed by retuning these constants.
* The language statement lives in five places (`LANGUAGE_COVERAGE_CONDITION`, `EXTERNAL_LLM_LANGUAGE_BLOCKED`, `_LANGUAGE_LIMIT`, `coverage["summary"]`, `renderLanguageCoverageNotice` in `templates/studio.html`); if consolidated, move the **fullest** (browser) wording into the shared source — never adopt the shortest.
* `privacy_profile["language_coverage"]` is the detection-time answer, written only where text was read (`analyze_privacy`, the OCR accept path, the backfill). `refresh_release_state` must **not** write its absent-key decision back onto the profile; it lives on `external_llm_gate["language_coverage"]`, which is what `refresh_document_state` and the review UPDATE in `blueprints/review.py` write `documents.language` from.
* Every read surface (`/api/document/<id>`, `/api/documents`, `/api/matter/<id>`) computes the release state through `released_state_for()` rather than deserializing the stored profile; do not put any back on the stored column. `residual_risk` stays the stored detection-time measurement.
* `python3 db/backfill_language_coverage.py` (`backfill_language_coverage` in `app.py`) is the **only** place a stored profile's language answer is recovered — a Python one-off, not a `.sql` migration, never run at startup, never `apply_reextraction()`. It rewrites residual risk only where coverage fails, and leaves a document whose text it cannot read entirely alone. Until it runs, a stale `Low` risk badge beside `Blocked` is cosmetic; the gate is correct.
* Every refusal `external_llm_gate_policy()` returns must start with "Blocked" (the dashboard chip, `benchmark.py` and `classify.py` select on that prefix).
* `tests/test_language_gate.py` pins all of the above; English detection rules exist but do not feed the gate — do not read the refusal as coverage, or the English findings as coverage either.

The two gates answer different questions and the UI must say which: the reviewed export reflects the reviewer's decisions (a deliberately retained identifier stays in it), while the external-LLM gate is clearance to send the document off the machine. Likewise `residual_risk` is the detection-time measurement the accuracy audit is calibrated on and `post_review_residual_risk` is the level the release gate was decided against — show both, never overwrite the first with the second.

A finding's `sample` must never contain a newline. Redaction is a string match against a DOCX part, whose text is the concatenation of its `w:t` runs, and `build_part_spans` inserts the paragraph breaks that `extraction` also sees — but a span the detector let run across a line break still matches nothing there, so it can never be removed from the deliverable however it was reviewed. Every detection rule bounds its trailing context with `[^\n]` for this reason; the person-name regexes use `[ \t]+` between name tokens (`privacy._NAME_SEP`, `turkish_names._SEP`). `tests/test_redaction_parity.py` asserts the invariant across the fixture corpus.

A finding must cover the WHOLE entity, not just the token that anchored the rule. Recall ("some finding overlaps it") and full-span recall ("every character is inside a finding") are different numbers, and the gap between them is text that ships: the court rules were anchored on the branch number, so `Ankara` in `Ankara 5. Asliye Hukuk Mahkemesi` lay outside every finding, could not be approved, and was delivered next to a redacted court — measured 0/16, and the bar rule left `Baro` out the same way. `accuracy_audit.py` cannot see this, because it matches a label against a finding by substring in either direction, so a fragment scores as recall 1.0. When you widen a rule's anchor, assert on offsets and then prove it on the exported file (`tests/test_court_full_span.py` runs the bundled sample through the real review and export endpoints and reads the `w:t` runs). Turkish casing is where such a prefix breaks: `İZMİR` folds to `izmir` only through `turkish_names.tr_fold`, and a rule running under `IGNORECASE` needs `(?-i:…)` around any class that is meant to stay case-sensitive (`privacy._COURT_PLACE_PREFIX`).

Counting capitalised words cannot tell `Ankara` from `Dosya`, so `_COURT_PLACE_PREFIX` only *captures* candidates and `privacy._venue_prefix_start` decides which of them are the venue's, against the bounded list in `legal_analyzer/turkish_places.py`. The same prefix is carried by every `court_or_authority` rule except the `Vergi Dairesi:` field-value rule -- including `GENERIC_COURT_SUFFIX_RULE`, which anchors on a bare institution word and so covers governorships and trade registries, `PROSECUTOR_OFFICE_RULE`, which names the prosecutor's office and ends at its name, and `DIRECTORATE_RULE`, which names the state directorates (tapu, nüfus, vergi dairesi, emniyet, SGK, milli eğitim) explicitly -- and by the bar-registration rule, so `İstanbul Cumhuriyet Başsavcılığı`, `Şişli Tapu Müdürlüğü` and `İstanbul Barosu Sicil No` are each one span. Do not replace the directorate list with a bare `müdürlüğü` trigger: that fires on company departments (`İNSAN KAYNAKLARI MÜDÜRLÜĞÜ` in `eval/corpus`) and, because the list gate walks right to left and rejects the subject word, never keeps the district anyway — the rule's comment has the measurement. That list is a span boundary only — no `DetectionRule` reads it and no finding is ever emitted for a place name. **The residual, stated plainly: a real Turkish place missing from the list still falls outside the span and still ships.** That is a bounded-list maintenance gap the held-out harness can quantify, and the repair is to add the place — from the Ministry courthouse directory the module cites, never from `eval/`, whose independence is the point of it. The list carries a second, opposite residual too (a district that is also a surname is absorbed into a venue span unless `privacy._venue_surname_mentions` can give it back to a person named in full in the same document); the module docstring says what remains, and a name blocklist is not the fix. There is deliberately no positional, line-position or word-count fallback: two rounds measured that absorbing ordinary words, and a third found an own-line escape reaching past a place the list had already recognised. The bundled sample's invented `Masalkent` is in `SYNTHETIC_CORPUS_PLACES` instead.

Two things hang off that boundary. `_named_court_spans` feeds `_generic_court_matches` the span each finding *claims*, not the raw match — and conversely `_generic_court_matches` decides suppression on its match's **institution anchor** (`match.end("court_place")`), never on its own widened start, because a named span can end between a place word and its institution word (the Yargıtay sweep is a fixed 60 characters), and a match judged "inside" on its widened start is re-found after the resume *without its city* — which then carries the other venue's placeholder — a captured word the list rejects is given back to nobody, and a raw-start window hid `Valiliği` inside no finding at all, unapprovable and therefore unredactable; the price is a nested duplicate finding, which `resolve_redaction_regions` clips. And `_assign_grouped_placeholders` — shared by courts and bar registrations, because a widened span put decoration in both their samples — keys the pseudonym on the folded **core** (a venue's branch number, a bar registration's digits), so a court named with its city once and bare afterwards is one identity — but it unifies a group only when the group carries at most one distinct place, because `Ankara 5. Asliye Hukuk Mahkemesi` and `İzmir 5. …` share a core and collapsing them would assert two cities' courts are one venue. That guard counts captured places, so a city missing from the list disarms it as well as leaking — the two collapse into one pseudonym. The place-prefix word class must also cover every character the place list uses: it omitted `â`, so twelve directory districts (`Hakkâri`, `Milâs`, …) could not be captured at all while `is_venue_place_token` still returned True, and `tests/test_court_full_span.py` now asserts that coverage over the whole list rather than over remembered names. The same "given back to nobody" rule binds the other claimant: `_person_spans` applies every person filter (address words, legal-form suffix) itself and `analyze_privacy` emits `natural_person_name` findings from exactly that list, because the place prefix hands a word back to a person span — so a span that is claimed but then discarded leaves the word inside no finding at all. Filter person spans in one place only; a second filter at the call site reopens it.

### Matter/document API surface (for orientation, not exhaustive)

Matters group documents; review/export flows operate per-document. See `README.md` for the full endpoint list (matters, OCR queue/run/accept/reject, PDF region CRUD/approve/reject/generate, redacted-export + QA for DOCX/PDF). When adding endpoints that touch findings, redaction, or export, follow the existing gate-checking pattern in `app.py` rather than introducing a new path around `refresh_release_state()`.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
