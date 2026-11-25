"""The design language carried from the approved Legal Redact landing page.

Three marks mean the same thing on every screen: a bracket around a finding
still in the text, a chip for the placeholder a redacted copy carries, and
the "1 [Find] 2 Decide 3 Export" header over the review. They are a visual
change only, so what is pinned here is what could quietly drift:

1. The header is a grouping of the studio's REAL steps, not a new workflow.
   Every step stepStates() returns sits under exactly one of the three, and
   "Complete & approve" sits under Export -- the reviewed copy is built once
   the redaction is marked complete and the review approved, which is how the
   landing page tells it.
2. Placeholders render as chips wherever the studio shows redacted output.
3. Finishing the document is step 3 of the review, not a disclosure at the
   bottom of the review column: the export checklist and download sit in a
   finish panel above the findings, and the header says what is left. It
   renders the server's export gate and never rebuilds it.

Everything is read from what the app serves: the studio page a reviewer is
sent, its own inline script run under Node against the real
/api/document/<id> and /redaction-plan responses. Nothing here reads a
template or a Python source file.

Everything is synthetic.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory

import tests.env_setup  # noqa: F401  -- sets LEGAL_ANALYZER_SECRET_KEY before app is imported
import app as app_module
from legal_analyzer.privacy import analyze_privacy
from tests.test_trust_surfaces import make_docx
from werkzeug.security import generate_password_hash

DOCX_ID = 1
PDF_ID = 2
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr",
}


class Node:
    def __init__(self, tag: str, attrs: dict[str, str], parent: "Node | None") -> None:
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[Node] = []
        self.text: list[str] = []

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def ancestors(self):
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def find(self, element_id: str) -> "Node":
        for node in self.walk():
            if node.attrs.get("id") == element_id:
                return node
        raise AssertionError(f"#{element_id} is not in the page")

    def with_class(self, name: str) -> list["Node"]:
        return [node for node in self.walk() if name in node.classes]

    def text_content(self) -> str:
        return "".join("".join(node.text) for node in self.walk())


class TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.current = self.root

    def handle_starttag(self, tag, attrs) -> None:
        node = Node(tag, {key: value or "" for key, value in attrs}, self.current)
        self.current.children.append(node)
        if tag not in VOID_TAGS:
            self.current = node

    def handle_startendtag(self, tag, attrs) -> None:
        self.current.children.append(Node(tag, {key: value or "" for key, value in attrs}, self.current))

    def handle_endtag(self, tag) -> None:
        node = self.current
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.current = node.parent

    def handle_data(self, data) -> None:
        self.current.text.append(data)


def parse(markup: str) -> Node:
    builder = TreeBuilder()
    builder.feed(markup)
    builder.close()
    return builder.root


# Runs the studio's inline script in a context with just enough of a DOM for
# its render functions, then reports what they produced.
HARNESS = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const elements = {};
function element(id) {
    if (!elements[id]) {
        const classes = new Set();
        elements[id] = {
            id, innerHTML: '', textContent: '', hidden: false, open: false, dataset: {},
            classList: {
                add: (name) => classes.add(name),
                remove: (name) => classes.delete(name),
                contains: (name) => classes.has(name),
                toggle: (name, on) => { const next = on === undefined ? !classes.has(name) : Boolean(on); if (next) classes.add(name); else classes.delete(name); return next; },
            },
            attributes: {},
            setAttribute(name, value) { this.attributes[name] = String(value); },
            getAttribute(name) { return this.attributes[name] ?? null; },
            addEventListener() {}, scrollIntoView() {}, focus() {},
            querySelector: () => null, querySelectorAll: () => [],
            getBoundingClientRect: () => ({ top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }),
        };
    }
    return elements[id];
}
const context = {
    console,
    document: {
        addEventListener() {},
        getElementById: element,
        querySelector: (selector) => element(`query:${selector}`),
        querySelectorAll: () => [],
        createElement: (tag) => element(`new:${tag}`),
    },
    window: { matchMedia: () => ({ matches: false }), addEventListener() {} },
    fetch: async (url) => ({ ok: true, json: async () => input.plan }),
    requestAnimationFrame: () => 0, cancelAnimationFrame() {},
    getComputedStyle: () => ({ display: 'none' }),
    setTimeout, clearTimeout,
};
context.window.document = context.document;
vm.createContext(context);
for (const source of input.scripts) vm.runInContext(source, context);
const run = (code) => vm.runInContext(code, context);
context.__doc = input.doc;
context.__plan = input.plan;
(async () => {
    run('currentDoc = __doc;');
    const out = {};
    out.stages = run('REVIEW_STAGES.map((stage) => stage.label)');
    out.steps = run('stepStates(__doc).map((step) => ({ stage: step.stage, label: step.label }))');
    out.finish = run('finishState(__doc)');
    out.fixIds = run('Object.keys(EXPORT_CONDITION_FIXES)');
    run('renderStepper(__doc)');
    out.stepper = element('workflowStepper').innerHTML;
    run('renderFinishPanel(__doc)');
    out.finishStatus = element('finishStatus').textContent;
    run('renderExportGate(__doc)');
    out.exportGate = element('exportGatePanel').innerHTML;
    out.canvas = run('buildDocHtml(__plan.segments || [])');
    await run('renderPreviewPane()');
    out.preview = element('previewText').innerHTML;
    process.stdout.write(JSON.stringify(out));
})().catch((error) => { console.error(error && error.stack || error); process.exit(1); });
"""


class ServedStudio:
    """A reviewer's studio for a pending DOCX and a pending PDF, as served."""

    def __init__(self) -> None:
        self.tmp = TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.db_path = tmp / "design.db"
        self.original_db_path = app_module.DB_PATH
        app_module.DB_PATH = self.db_path
        app_module.READY_DB_PATHS.discard(self.db_path.resolve())

        conn = sqlite3.connect(self.db_path)
        conn.executescript(Path("db/schema.sql").read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            ("reviewer", generate_password_hash("secret"), "reviewer"),
        )
        text = (
            "Av. Ayse Demir TCKN 10000000146 tarafindan sunulmustur."
            " Isbu dilekce ile taraflar arasinda imzalanan sozlesme hukumleri geregi alacagin tahsili"
            " talep edilmektedir. Bu nedenle ilgili tum bolumler yukarida aciklanmistir."
        )
        docx_path = tmp / "petition.docx"
        make_docx(docx_path, text)
        profile = json.dumps(analyze_privacy("petition.docx", text))
        conn.execute(
            """
            INSERT INTO documents (id, filename, filepath, file_extension, file_size, title, language,
                                   extraction_status, privacy_profile, ocr_status)
            VALUES (?, 'petition.docx', ?, '.docx', ?, 'petition', 'TR', 'Complete', ?, 'not_required')
            """,
            (DOCX_ID, str(docx_path), docx_path.stat().st_size, profile),
        )
        # Accepted OCR with no coordinate tokens: the one state in which the
        # PDF gate carries its last, conditional condition.
        conn.execute(
            """
            INSERT INTO documents (id, filename, filepath, file_extension, file_size, title, language,
                                   extraction_status, privacy_profile, ocr_status)
            VALUES (?, 'scan.pdf', ?, '.pdf', 100, 'scan', 'TR', 'Complete', ?, 'accepted')
            """,
            (PDF_ID, str(tmp / "scan.pdf"), profile),
        )
        conn.execute(
            """
            INSERT INTO privacy_findings (document_id, category, sample, risk, recommended_action, placeholder,
                                          replacement_text, review_status, source, fingerprint)
            VALUES (?, 'natural_person_name', 'Ayse Demir', 'HIGH', 'Pseudonymize', '[PERSON_1]', '[PERSON_1]', 'approved', 'detector', 'fp1'),
                   (?, 'turkish_national_id', '10000000146', 'CRITICAL', 'Redact', '[TCKN_1]', '[TCKN_REDACTED]', 'pending', 'detector', 'fp2'),
                   (?, 'natural_person_name', 'Ayse Demir', 'HIGH', 'Pseudonymize', '[PERSON_1]', '[PERSON_1]', 'pending', 'detector', 'fp3'),
                   (?, 'turkish_national_id', '10000000146', 'CRITICAL', 'Redact', '[TCKN_1]', '[TCKN_REDACTED]', 'pending', 'detector', 'fp4')
            """,
            (DOCX_ID, DOCX_ID, PDF_ID, PDF_ID),
        )
        conn.execute(
            """
            INSERT INTO pdf_redaction_regions (document_id, page_number, x0, y0, x1, y1, category, source, review_status)
            VALUES (?, 1, 10, 10, 50, 20, 'natural_person_name', 'manual', 'pending')
            """,
            (PDF_ID,),
        )
        conn.commit()
        conn.close()

        self.client = app_module.app.test_client()
        response = self.client.post("/login", data={"username": "reviewer", "password": "secret"})
        assert response.status_code == 302, "synthetic reviewer could not sign in"

    def get(self, route: str):
        response = self.client.get(route)
        assert response.status_code == 200, f"{route} returned {response.status_code}"
        return response

    def page(self, doc_id: int) -> str:
        return self.get(f"/studio/{doc_id}").get_data(as_text=True)

    def document(self, doc_id: int) -> dict:
        return self.get(f"/api/document/{doc_id}").json

    def plan(self, doc_id: int) -> dict:
        return self.get(f"/api/document/{doc_id}/redaction-plan?style=placeholder").json

    def scripts(self, markup: str) -> list[str]:
        """The page's own scripts in load order: escape.js, then the inline one."""
        tree = parse(markup)
        sources = []
        for node in tree.walk():
            if node.tag != "script":
                continue
            src = node.attrs.get("src")
            if src is None:
                sources.append("".join(node.text))
            elif src.endswith("/escape.js"):
                response = self.get(src)
                sources.append(response.get_data(as_text=True))
                response.close()
        return sources

    def close(self) -> None:
        app_module.DB_PATH = self.original_db_path
        app_module.READY_DB_PATHS.discard(self.db_path.resolve())
        self.tmp.cleanup()


class TheFinishPanelSitsInTheReviewColumnTests(unittest.TestCase):
    """Where finishing the document lives in the page a reviewer is served."""

    @classmethod
    def setUpClass(cls) -> None:
        studio = ServedStudio()
        try:
            cls.tree = parse(studio.page(DOCX_ID))
        finally:
            studio.close()

    def test_it_sits_above_the_findings_in_the_review_assistant(self) -> None:
        panel = self.tree.find("finishPanel")
        pane = self.tree.find("reviewPane")
        column = next(node for node in panel.ancestors() if node.tag == "aside")
        self.assertIn("review-assistant", column.classes)
        self.assertIn(column, list(pane.ancestors()))
        order = [node for node in column.walk() if node in (panel, pane)]
        self.assertEqual(order, [panel, pane])

    def test_the_checklist_and_download_are_not_behind_a_disclosure(self) -> None:
        gate = self.tree.find("exportGatePanel")
        self.assertIn(self.tree.find("finishPanel"), list(gate.ancestors()))
        self.assertNotIn("details", [node.tag for node in gate.ancestors()])

    def test_the_technical_detail_stays_one_disclosure_away_inside_it(self) -> None:
        details = self.tree.find("exportReadinessDetails")
        self.assertEqual(details.tag, "details")
        self.assertIn(self.tree.find("finishPanel"), list(details.ancestors()))
        for element in ("releaseGatePanel", "exportLinks", "qaReport", "artifactTimeline", "reviewActionsPanel"):
            with self.subTest(element=element):
                self.assertIn(details, list(self.tree.find(element).ancestors()))


@unittest.skipUnless(shutil.which("node"), "node is not installed; the studio's script is not exercised")
class TheStudioScriptTests(unittest.TestCase):
    """The studio's own inline script, run against the real API responses."""

    @classmethod
    def setUpClass(cls) -> None:
        studio = ServedStudio()
        try:
            cls.docs = {doc_id: studio.document(doc_id) for doc_id in (DOCX_ID, PDF_ID)}
            cls.results = {}
            for doc_id in (DOCX_ID, PDF_ID):
                payload = {
                    "scripts": studio.scripts(studio.page(doc_id)),
                    "doc": cls.docs[doc_id],
                    "plan": studio.plan(doc_id) if doc_id == DOCX_ID else {"has_text": False, "segments": []},
                }
                result = subprocess.run(
                    [shutil.which("node"), "-e", HARNESS],
                    input=json.dumps(payload),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if result.returncode != 0:
                    raise AssertionError(result.stderr)
                cls.results[doc_id] = json.loads(result.stdout)
        finally:
            studio.close()

    def test_the_header_reads_find_decide_export(self) -> None:
        for doc_id, result in self.results.items():
            with self.subTest(doc=doc_id):
                self.assertEqual(result["stages"], ["Find", "Decide", "Export"])
                header = parse(result["stepper"])
                labels = [node.text_content() for node in header.with_class("stage-label")]
                self.assertEqual(labels, ["Find", "Decide", "Export"])

    def test_every_real_step_sits_under_one_of_them(self) -> None:
        self.assertEqual(
            self.results[PDF_ID]["steps"],
            [
                {"stage": "find", "label": "Detect"},
                {"stage": "decide", "label": "Review boxes"},
                {"stage": "decide", "label": "Review findings"},
                {"stage": "export", "label": "Complete & approve"},
                {"stage": "export", "label": "Export & QA"},
            ],
        )
        self.assertEqual(
            [step["label"] for step in self.results[DOCX_ID]["steps"]],
            ["Detect", "Review findings", "Complete & approve", "Export & QA"],
        )

    def test_the_header_marks_the_step_the_review_is_on(self) -> None:
        header = parse(self.results[PDF_ID]["stepper"])
        current = [node for node in header.walk() if node.attrs.get("aria-current") == "step"]
        self.assertEqual([node.text_content().strip()[:len("2Decide")] for node in current], ["2Decide"])
        substeps = [node.text_content() for node in header.with_class("stage-substep")]
        self.assertEqual(substeps, ["Review boxes", "Review findings"])

    def test_the_header_and_the_panel_say_what_is_left(self) -> None:
        for doc_id, left in ((DOCX_ID, 1), (PDF_ID, 2)):
            with self.subTest(doc=doc_id):
                result = self.results[doc_id]
                expected = f"{left} finding{'' if left == 1 else 's'} left to decide"
                self.assertEqual(self.docs[doc_id]["pending_finding_count"], left)
                self.assertEqual(result["finish"]["text"], expected)
                self.assertEqual(result["finishStatus"], expected)
                header = parse(result["stepper"])
                self.assertEqual([node.text_content() for node in header.with_class("stage-left-long")], [expected])

    def test_the_checklist_is_the_servers_export_gate(self) -> None:
        for doc_id, result in self.results.items():
            with self.subTest(doc=doc_id):
                conditions = self.docs[doc_id]["export_gate"]["conditions"]
                rows = parse(result["exportGate"]).with_class("finish-row")
                self.assertEqual(
                    [row.with_class("gate-label")[0].text_content() for row in rows],
                    [condition["label"] for condition in conditions],
                )
                self.assertEqual(
                    ["is-pass" in row.classes for row in rows],
                    [bool(condition["passed"]) for condition in conditions],
                )

    def test_a_fix_is_offered_only_beside_an_unmet_condition(self) -> None:
        for doc_id, result in self.results.items():
            with self.subTest(doc=doc_id):
                conditions = self.docs[doc_id]["export_gate"]["conditions"]
                for row, condition in zip(parse(result["exportGate"]).with_class("finish-row"), conditions):
                    if condition["passed"]:
                        self.assertEqual(row.with_class("finish-fix"), [], condition["id"])

    def test_every_fix_is_keyed_to_a_condition_the_server_emits(self) -> None:
        emitted = {
            condition["id"] for doc in self.docs.values() for condition in doc["export_gate"]["conditions"]
        }
        for result in self.results.values():
            self.assertTrue(result["fixIds"])
            self.assertLessEqual(set(result["fixIds"]), emitted)

    def test_the_canvas_sets_an_approved_finding_as_a_chip_and_brackets_the_rest(self) -> None:
        canvas = parse(self.results[DOCX_ID]["canvas"])
        self.assertEqual([node.text_content() for node in canvas.with_class("ph-chip")], ["[PERSON_1]"])
        marks = canvas.with_class("mark")
        self.assertEqual([node.text_content() for node in marks], ["10000000146"])
        self.assertIn("mark--pending", marks[0].classes)
        self.assertNotIn("Ayse Demir", canvas.text_content())

    def test_the_redacted_preview_sets_its_placeholders_as_chips(self) -> None:
        preview = parse(self.results[DOCX_ID]["preview"])
        self.assertEqual([node.text_content() for node in preview.with_class("ph-chip")], ["[PERSON_1]"])
        self.assertNotIn("Ayse Demir", preview.text_content())


if __name__ == "__main__":
    unittest.main()
