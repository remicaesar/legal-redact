"""Compute everything the /welcome demo draws, from the product itself.

Run from the repository root with the repo's own interpreter, then rebuild:

    .venv/bin/python landing/scripts/gen_demo.py
    cd landing && npm run build

It writes landing/src/demo.json (what the page draws) and
landing/scripts/plan_cases.json (what scripts/verify_plan.mjs checks the page's
JavaScript against). tests/test_landing.py calls generate() and fails when the
committed files no longer match what the product produces, so a detection change
cannot silently leave the public page showing findings the rules no longer make.

Nothing in the demo is typed by hand. generate() drives the real Flask app
through its test client, against a throwaway SQLite database in a temporary
directory, the way a reviewer's browser would:

  1. POST /api/upload                    the bundled synthetic petition, with one
                                         paragraph removed (see VARIANT below)
  2. GET  /api/document/<id>/text        the text detection ran on
  3. read the findings the upload stored (offsets, category, risk, placeholder)
  4. scripted review: keep the defendant company's name, approve the other
     findings, mark redaction complete, approve the document; read both gates
     and build the reviewed DOCX, then read its text back
  5. reset, approve everything, same again

VARIANT. The page's headline says "Nothing becomes [PERSON_1] until you decide",
so the plaintiff must be [PERSON_1] in the demo. In the shipped sample it is
[PERSON_2]: privacy._person_spans collects title-prefixed names (counsel, "Av.
Ekin Düşsel") before role-prefixed ones ("Davacı: Deniz Kurgusal"), and
placeholders are numbered in that order. So the demo uploads the sample with the
"Vekili: Av. ..." paragraph removed, built in the temporary directory; the repo's
file is untouched. Nothing else in the document changes.

MASKING. The sample's TCKN and VKN were randomly generated and pass check-digit
validation, so a match with a real number cannot be excluded (samples/README.md).
The app detects the full numbers; everything this script returns carries them
with their middle digits replaced by "•", character for character, so every
offset still holds. It refuses to return if either full number survives.

The repo is only read. The database, uploads and exports live in a
TemporaryDirectory that is deleted on exit, and the app module's paths are put
back afterwards.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import sys
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

LANDING = Path(__file__).resolve().parent.parent
REPO = LANDING.parent
DEMO_JSON = LANDING / "src" / "demo.json"
PLAN_CASES_JSON = LANDING / "scripts" / "plan_cases.json"

SAMPLE = "turkish_petition.docx"
DROP_PARAGRAPH = "Vekili: Av."  # the one paragraph the demo variant removes
MASK = "•"  # "•"
MASKED_CATEGORIES = {"turkish_national_id", "tax_number"}
CASES = 120
# One letter per finding, in the order demo.json lists them.
DECISION_CODES = {"approved": "a", "retained": "r", "dismissed": "d", "pending": "p"}
SEGMENT_CODES = {"text": "t", "marked": "m", "redacted": "r"}


class GenerationError(RuntimeError):
    pass


def fail(msg: str) -> None:
    raise GenerationError(msg)


def docx_paragraphs(data: bytes) -> list[str]:
    with ZipFile(BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    paras = re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S)
    out = []
    for p in paras:
        t = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p))
        if t.strip():
            out.append(t)
    return out


def variant_docx(data: bytes) -> bytes:
    """The sample with the counsel paragraph removed; every other part as shipped."""
    out = BytesIO()
    with ZipFile(BytesIO(data)) as zin, ZipFile(out, "w") as zout:
        for info in zin.infolist():
            body = zin.read(info.filename)
            if info.filename == "word/document.xml":
                xml = body.decode("utf-8")
                paras = re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S)
                hits = [p for p in paras if DROP_PARAGRAPH in "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p))]
                if len(hits) != 1:
                    fail(f"expected one '{DROP_PARAGRAPH}' paragraph, found {len(hits)}")
                body = xml.replace(hits[0], "", 1).encode("utf-8")
            zout.writestr(info, body)
    return out.getvalue()


def gate_of(detail: dict) -> dict:
    gate = detail["privacy_profile"]["external_llm_gate"]
    return {
        "allowed": gate["allowed"],
        "decision": gate["decision"],
        "conditions": [
            {"id": c["id"], "label": c["label"], "passed": bool(c["passed"]), "detail": c["detail"]}
            for c in gate["conditions"]
        ],
        "residual_after_review": detail["privacy_profile"]["post_review_residual_risk"]["level"],
    }


def export_gate_of(detail: dict) -> dict:
    eg = detail["export_gate"]
    return {
        "allowed": eg["allowed"],
        "conditions": [
            {"id": c["id"], "label": c["label"], "passed": bool(c["passed"]), "detail": c["detail"]}
            for c in eg["conditions"]
        ],
    }


@contextmanager
def throwaway_app():
    """A test client on a fresh database in a temp dir; the app's paths are restored after."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import tests  # noqa: F401  -- CSRF-aware test client; sets the secret key
    import app as app_module
    from werkzeug.security import generate_password_hash

    from legal_analyzer.taxonomy import CATEGORIES, SUBCATEGORIES

    saved = (app_module.DB_PATH, app_module.UPLOAD_DIR, app_module.EXPORT_DIR)
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = root / "demo.sqlite3"
        conn = sqlite3.connect(db)
        conn.executescript((REPO / "db/schema.sql").read_text(encoding="utf-8"))
        for name, name_tr, icon in CATEGORIES:
            conn.execute("INSERT OR IGNORE INTO categories (name, name_tr, icon) VALUES (?, ?, ?)", (name, name_tr, icon))
            cid = conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()[0]
            for sn, snt in SUBCATEGORIES[name]:
                conn.execute(
                    "INSERT OR IGNORE INTO subcategories (category_id, name, name_tr) VALUES (?, ?, ?)", (cid, sn, snt)
                )
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            ("demo-reviewer", generate_password_hash("demo"), "reviewer"),
        )
        conn.commit()
        conn.close()
        app_module.DB_PATH = db
        app_module.READY_DB_PATHS.discard(db.resolve())
        app_module.UPLOAD_DIR = root / "uploads"
        app_module.EXPORT_DIR = root / "exports"
        try:
            client = app_module.app.test_client()
            if client.post("/login", json={"username": "demo-reviewer", "password": "demo"}).status_code != 200:
                fail("login failed")
            yield client
        finally:
            app_module.DB_PATH, app_module.UPLOAD_DIR, app_module.EXPORT_DIR = saved
            app_module.READY_DB_PATHS.discard(db.resolve())


def generate() -> tuple[dict, dict]:
    """Return (demo, plan_cases), exactly as main() writes them."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from legal_analyzer import privacy
    from legal_analyzer.docx_redactor import (
        Occurrence,
        expand_to_word_bounds,
        find_case_insensitive,
        purge_target_pattern_cache,
    )
    from legal_analyzer.redaction_plan import plan_segments, plan_text
    from legal_analyzer.taxonomy import SUPPORTED_EXTENSIONS

    with throwaway_app() as c:

        def get(url: str) -> dict:
            r = c.get(url)
            if r.status_code != 200:
                fail(f"GET {url}: {r.status_code}")
            return r.json

        def post(url: str, body: dict) -> dict:
            r = c.post(url, json=body)
            if r.status_code != 200:
                fail(f"POST {url} {body}: {r.status_code} {r.json}")
            return r.json

        def export_paragraphs(doc_id: int) -> list[str]:
            r = c.get(f"/api/document/{doc_id}/redacted-export?format=docx")
            if r.status_code != 200:
                fail(f"export {doc_id}: {r.status_code} {r.json}")
            return docx_paragraphs(r.data)

        # ---- 1. upload -------------------------------------------------------
        data = variant_docx((REPO / "samples/documents" / SAMPLE).read_bytes())
        r = c.post("/api/upload", data={"file": (BytesIO(data), SAMPLE)}, content_type="multipart/form-data")
        if r.status_code != 200:
            fail(f"upload: {r.status_code} {r.json}")
        uploaded = r.json
        doc_id = uploaded["id"]
        if uploaded["extraction_status"] != "Complete":
            fail(f"extraction degraded: {uploaded['extraction_status']} {uploaded.get('extraction_warning')}")
        text = get(f"/api/document/{doc_id}/text").get("text") or ""
        if not text:
            fail("no text")
        if [line for line in text.split("\n") if line.strip()] != docx_paragraphs(data):
            fail("extracted text does not match the source DOCX paragraphs")

        raw_findings = sorted(uploaded["findings"], key=lambda f: (f["start_offset"], -f["end_offset"]))
        findings = []
        for f in raw_findings:
            s, e = f["start_offset"], f["end_offset"]
            if s is None or e is None or text[s:e] != f["sample"]:
                fail(f"finding {f['id']} offsets do not frame its sample")
            if (f.get("review_status") or "pending") != "pending":
                fail("a finding arrived already decided")
            # [start, end, raw width, own]: the finding's own position first,
            # then every other match of its sample (the backstop), as
            # docx_redactor.resolve_redaction builds them.
            matches = []
            own = Occurrence.at(text, f["sample"], s).locate(text, f["sample"])
            matches.append([*expand_to_word_bounds(text, *own), own[1] - own[0], 1])
            for raw_start, raw_end in find_case_insensitive(text, f["sample"]):
                if (raw_start, raw_end) == own:
                    continue
                start, end = expand_to_word_bounds(text, raw_start, raw_end)
                matches.append([start, end, raw_end - raw_start, 0])
            findings.append(
                {
                    "id": f["id"],
                    "category": f["category"],
                    "label": privacy.explain_category(f["category"])["label"],
                    "basis": privacy.explain_category(f["category"])["basis"],
                    "risk": f["risk"],
                    "direct": f["category"] in privacy.DIRECT_IDENTIFIER_CATEGORIES,
                    "placeholder": f["placeholder"],
                    "sample": f["sample"],
                    "start": s,
                    "end": e,
                    "matches": matches,
                }
            )
        purge_target_pattern_cache()

        persons = [f for f in findings if f["category"] == "natural_person_name"]
        if not persons or any(f["placeholder"] != "[PERSON_1]" for f in persons):
            fail(f"the plaintiff is not [PERSON_1]: {[(f['sample'], f['placeholder']) for f in persons]}")

        uploaded_detail = get(f"/api/document/{doc_id}")
        gate_uploaded = gate_of(uploaded_detail)
        export_uploaded = export_gate_of(uploaded_detail)

        # ---- 2. scripted review: keep the defendant company's name -----------
        companies = [f for f in findings if f["category"] == "company_name"]
        if len(companies) != 1:
            fail(f"expected one company finding, got {[f['sample'] for f in companies]}")
        kept = companies[0]
        if not kept["direct"]:
            fail("the kept finding must be a direct identifier for the gate note to hold")
        post(f"/api/finding/{kept['id']}/review", {"action": "retain"})
        post(f"/api/document/{doc_id}/findings/review-batch", {"action": "approve", "only_pending": True})
        post(f"/api/document/{doc_id}/review", {"action": "mark_redacted"})
        post(f"/api/document/{doc_id}/review", {"action": "approve"})
        kept_detail = get(f"/api/document/{doc_id}")
        kept_statuses = {f["id"]: f["review_status"] for f in kept_detail["findings"]}
        kept_export = export_paragraphs(doc_id)

        # ---- 3. everything approved --------------------------------------------
        post(f"/api/document/{doc_id}/review", {"action": "reset"})
        post(f"/api/finding/{kept['id']}/review", {"action": "approve"})
        post(f"/api/document/{doc_id}/review", {"action": "mark_redacted"})
        post(f"/api/document/{doc_id}/review", {"action": "approve"})
        all_detail = get(f"/api/document/{doc_id}")
        all_statuses = {f["id"]: f["review_status"] for f in all_detail["findings"]}
        all_export = export_paragraphs(doc_id)

    def rows_of(statuses: dict[int, str]) -> list[dict]:
        return [dict(f, review_status=statuses[f["id"]]) for f in raw_findings]

    def plan_of(statuses: dict[int, str]) -> str:
        return plan_text(text, rows_of(statuses), privacy.REDACTED_REVIEW_STATUSES)

    # The plan and the delivered DOCX must be the same document.
    for label, statuses, exported in (("kept", kept_statuses, kept_export), ("all", all_statuses, all_export)):
        if [line for line in plan_of(statuses).split("\n") if line.strip()] != exported:
            fail(f"{label}: redaction plan disagrees with the exported DOCX")

    # ---- 4. cases for the page's port of the plan (scripts/verify_plan.mjs) --
    rng = random.Random(8)
    decision_sets = [{f["id"]: rng.choice(list(DECISION_CODES)) for f in findings} for _ in range(CASES)]
    decision_sets += [kept_statuses, all_statuses, {f["id"]: "pending" for f in findings}]
    cases = []
    for statuses in decision_sets:
        segments = plan_segments(text, rows_of(statuses), privacy.REDACTED_REVIEW_STATUSES)
        cases.append(
            {
                "decisions": "".join(DECISION_CODES[statuses[f["id"]]] for f in findings),
                "segments": [[SEGMENT_CODES[s.kind], s.start, s.end, s.finding_id] for s in segments],
            }
        )
    purge_target_pattern_cache()

    counts: dict[str, int] = {}
    for f in findings:
        counts[f["label"]] = counts.get(f["label"], 0) + 1

    demo = {
        "source": {
            "file": f"samples/documents/{SAMPLE}",
            "variant": f"paragraph starting '{DROP_PARAGRAPH}' removed",
            "masked": sorted(MASKED_CATEGORIES),
            "extension": uploaded["file_extension"],
            "extraction_status": uploaded["extraction_status"],
            "language": uploaded["language"],
            "language_label": uploaded_detail["privacy_profile"]["external_llm_gate"]["language_coverage"]["label"],
            "residual_risk_at_detection": uploaded["residual_risk"],
        },
        "text": text,
        "findings": findings,
        "category_counts": counts,
        "direct_count": sum(1 for f in findings if f["direct"]),
        "kept_id": kept["id"],
        "states": {
            "uploaded": {"gate": gate_uploaded, "export_gate": export_uploaded},
            "kept": {
                "statuses": kept_statuses,
                "gate": gate_of(kept_detail),
                "export_gate": export_gate_of(kept_detail),
                "export_paragraphs": kept_export,
                "kept_name_in_export": kept["sample"] in "\n".join(kept_export),
            },
            "all": {
                "statuses": all_statuses,
                "gate": gate_of(all_detail),
                "export_gate": export_gate_of(all_detail),
                "export_paragraphs": all_export,
                "samples_left_in_export": sum(1 for f in findings if f["sample"] in "\n".join(all_export)),
            },
        },
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
    }
    if len(gate_uploaded["conditions"]) != 8:
        fail(f"the release gate has {len(gate_uploaded['conditions'])} conditions; the page's copy says 8")

    # ---- 5. mask the ID numbers in everything returned ---------------------------
    secrets = [re.sub(r"\D", "", f["sample"]) for f in findings if f["category"] in MASKED_CATEGORIES]
    if len(secrets) != len(MASKED_CATEGORIES):
        fail(f"expected one TCKN and one VKN, got {len(secrets)}")
    chars = list(text)
    for f in findings:
        if f["category"] in MASKED_CATEGORIES:
            digits = [i for i in range(f["start"], f["end"]) if text[i].isdigit()]
            for i in digits[3:-2]:
                chars[i] = MASK
    masked_text = "".join(chars)
    for f in findings:
        f["sample"] = masked_text[f["start"] : f["end"]]
        f["masked"] = f["category"] in MASKED_CATEGORIES
    for state in ("kept", "all"):
        paras = demo["states"][state]["export_paragraphs"]
        for raw in secrets:
            paras[:] = [p.replace(raw, raw[:3] + MASK * (len(raw) - 5) + raw[-2:]) for p in paras]
    demo["text"] = masked_text
    plan_cases = {"text": masked_text, "cases": cases}
    serialized = dumps(demo) + dumps(plan_cases)
    for raw in secrets:
        if raw in serialized:
            fail("a full ID number would be written; masking failed")
    # Exactly what main() writes (JSON object keys are strings), so a caller can
    # compare it with the committed files.
    return json.loads(dumps(demo)), json.loads(dumps(plan_cases))


def dumps(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1) + "\n"


def dumps_cases(plan_cases: dict) -> str:
    """plan_cases.json, one case per line: small, and a changed case is one diff line."""
    lines = [json.dumps(case, ensure_ascii=False, separators=(",", ":")) for case in plan_cases["cases"]]
    return (
        '{"text": ' + json.dumps(plan_cases["text"], ensure_ascii=False) + ',\n "cases": [\n'
        + ",\n".join(lines) + "\n]}\n"
    )


def main() -> None:
    # app.py loads the repo's .env when python-dotenv is importable. The demo needs
    # nothing from it, so the import is made to fail and the file is never read.
    sys.modules.setdefault("dotenv", None)
    try:
        demo, plan_cases = generate()
    except GenerationError as exc:
        sys.exit(f"gen_demo: {exc}")
    DEMO_JSON.write_text(dumps(demo), encoding="utf-8")
    PLAN_CASES_JSON.write_text(dumps_cases(plan_cases), encoding="utf-8")
    gates = {name: demo["states"][name]["gate"]["allowed"] for name in ("uploaded", "kept", "all")}
    print(
        f"gen_demo: {len(demo['findings'])} findings ({demo['direct_count']} direct); gate allowed {gates}; "
        f"{len(plan_cases['cases'])} plan cases -> {PLAN_CASES_JSON.relative_to(REPO)}"
    )


if __name__ == "__main__":
    main()
