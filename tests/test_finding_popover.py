"""A finding is decided in a card under its bracket, not in the review column.

The review column used to carry a guided card and a list of full decision
cards. A finding is now decided where it is read: clicking its bracket (or
its box, on a PDF) opens a card under it, and the column keeps only the
whole-document decisions plus an entry for any finding the canvas cannot
show. "Mark redaction complete" moved from the step bar to the end of the
document's status line.

What is pinned here is what that move could quietly cost, run through the
studio's own inline script under Node against the page and API responses a
reviewer is served:

1. The column renders no per-finding decision, and every decision -- the
   box decisions included -- is on the card.
2. "Mark redaction complete" is on the status line and not in the step bar.
3. The findings navigation lands in the review column, and a load failure
   is written somewhere a reviewer can see it.
4. A decision shortcut acts only on a card that is on screen, and with no
   card it reaches only a box a reviewer drew -- never a box that covers a
   detected finding, whose rejection leaves that identifier in the file.

Everything is synthetic.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import unittest

from tests.test_design_language import DOCX_ID, PDF_ID, Node, ServedStudio, parse

HARNESS = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const log = { fetches: [], scrolled: [], focused: [] };
const elements = {};
const queryAll = {};
const listeners = {};
function element(id) {
    if (!elements[id]) {
        const classes = new Set();
        elements[id] = {
            id, innerHTML: '', textContent: '', hidden: false, open: false, dataset: {}, style: {},
            rects: [], offsetWidth: 300, offsetHeight: 200, isConnected: true,
            classList: {
                add: (name) => classes.add(name),
                remove: (name) => classes.delete(name),
                contains: (name) => classes.has(name),
                toggle: (name, on) => { const next = on === undefined ? !classes.has(name) : Boolean(on); if (next) classes.add(name); else classes.delete(name); return next; },
            },
            attributes: {},
            setAttribute(name, value) { this.attributes[name] = String(value); },
            getAttribute(name) { return this.attributes[name] ?? null; },
            hasAttribute(name) { return name in this.attributes; },
            addEventListener() {}, appendChild() {}, remove() {},
            clientWidth: 600, clientHeight: 800, options: [], value: "",
            scrollIntoView() { log.scrolled.push(this.id); },
            focus() { log.focused.push(this.id); },
            contains: () => false, closest: () => null,
            querySelector: () => null, querySelectorAll: () => [],
            getClientRects() { return this.rects; },
            getBoundingClientRect: () => ({ top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }),
        };
    }
    return elements[id];
}
const context = {
    console,
    document: {
        documentElement: { clientWidth: 1200, clientHeight: 800 },
        body: element('body'),
        addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
        getElementById: element,
        querySelector: (selector) => element(`query:${selector}`),
        querySelectorAll: (selector) => queryAll[selector] || [],
        createElement: (tag) => element(`new:${tag}`),
    },
    window: {
        matchMedia: () => ({ matches: false }), addEventListener() {}, scrollBy() {},
        innerWidth: 1200, innerHeight: 800, confirm: () => true,
    },
    fetch: (url, options) => {
        log.fetches.push({ url, body: options && options.body ? JSON.parse(options.body) : null });
        return new Promise(() => {});
    },
    requestAnimationFrame: () => 0, cancelAnimationFrame() {},
    getComputedStyle: () => ({ display: 'none', overflowY: 'visible' }),
    setTimeout, clearTimeout,
};
context.window.document = context.document;
context.element = element;
context.queryAll = queryAll;
context.press = (key) => (listeners.keydown || []).forEach((listener) => listener({
    key, target: { tagName: 'BODY', isContentEditable: false },
    metaKey: false, ctrlKey: false, altKey: false, preventDefault() {},
}));
vm.createContext(context);
for (const source of input.scripts) vm.runInContext(source, context);
log.fetches.length = 0;
log.scrolled.length = 0;
log.focused.length = 0;
context.__input = input;
context.__log = log;
const run = (code) => vm.runInContext(code, context);
run('currentDoc = __input.doc; pdfRegions = __input.regions;');
(async () => {
    const result = await run(`(async () => { ${input.scenario} })()`);
    process.stdout.write(JSON.stringify({ result, log }));
})().catch((error) => { console.error(error && error.stack || error); process.exit(1); });
"""


def hidden_in(tree: Node, element_id: str) -> bool:
    """Whether the served page hides the element, or anything around it."""
    node = tree.find(element_id)
    return any("hidden" in item.attrs for item in [node, *node.ancestors()])


@unittest.skipUnless(shutil.which("node"), "node is not installed; the studio's script is not exercised")
class FindingCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        studio = ServedStudio()
        try:
            pdf = studio.document(PDF_ID)
            cls.linked_finding = pdf["findings"][0]["id"]
            conn = sqlite3.connect(studio.db_path)
            cls.linked_region = conn.execute(
                """
                INSERT INTO pdf_redaction_regions (document_id, finding_id, page_number, x0, y0, x1, y1,
                                                   category, source, review_status)
                VALUES (?, ?, 1, 60, 10, 120, 20, 'turkish_national_id', 'generated', 'pending')
                """,
                (PDF_ID, cls.linked_finding),
            ).lastrowid
            conn.commit()
            conn.close()
            cls.pages = {doc_id: studio.page(doc_id) for doc_id in (DOCX_ID, PDF_ID)}
            cls.trees = {doc_id: parse(markup) for doc_id, markup in cls.pages.items()}
            cls.scripts = {doc_id: studio.scripts(markup) for doc_id, markup in cls.pages.items()}
            cls.docs = {doc_id: studio.document(doc_id) for doc_id in (DOCX_ID, PDF_ID)}
            cls.regions = {
                doc_id: studio.get(f"/api/document/{doc_id}/pdf/regions").json["regions"] if doc_id == PDF_ID else []
                for doc_id in (DOCX_ID, PDF_ID)
            }
            cls.manual_region = next(region["id"] for region in cls.regions[PDF_ID] if not region["finding_id"])
            cls.docx_plan = studio.plan(DOCX_ID)
            for finding in cls.docs[DOCX_ID]["findings"]:
                response = studio.client.post(f"/api/finding/{finding['id']}/review", json={"action": "approve"})
                assert response.status_code == 200, response.get_data(as_text=True)
            cls.decided_docx = studio.document(DOCX_ID)
        finally:
            studio.close()

    def run_studio(self, doc_id: int, scenario: str, doc: dict | None = None) -> dict:
        payload = {
            "scripts": self.scripts[doc_id],
            "doc": doc or self.docs[doc_id],
            "regions": self.regions[doc_id],
            "plan": self.docx_plan,
            "scenario": scenario,
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
        return json.loads(result.stdout)

    def docx_card_scenario(self, finding_id: int, drawn: bool, keys: str) -> str:
        span = (
            f"const span = element('span'); span.dataset.findingId = '{finding_id}';"
            " span.classList.add('doc-span');"
            " span.rects = [{ left: 100, right: 200, top: 100, bottom: 120, width: 100, height: 20 }];"
            " queryAll['#docPaper .doc-span[data-finding-id]'] = [span];"
        ) if drawn else ""
        return f"""
            {span}
            selectedFindingId = {finding_id};
            findingPopover.open = true;
            findingPopover.findingId = {finding_id};
            findingPopover.anchor = {{ kind: 'doc' }};
            element('assistantCurrentFinding').hidden = false;
            {''.join(f"press({json.dumps(key)});" for key in keys)}
        """

    def pending_docx_finding(self) -> int:
        return next(finding["id"] for finding in self.docs[DOCX_ID]["findings"] if finding["review_status"] == "pending")

    def card_and_other_docx_finding(self) -> tuple[int, int]:
        shown = self.pending_docx_finding()
        other = next(finding["id"] for finding in self.docs[DOCX_ID]["findings"] if finding["id"] != shown)
        return shown, other

    # 1. The column decides the whole document; the card decides a finding.

    def test_the_card_is_not_in_the_review_column(self) -> None:
        tree = self.trees[DOCX_ID]
        column = next(node for node in tree.find("reviewPane").ancestors() if node.tag == "aside")
        self.assertNotIn(tree.find("assistantCurrentFinding"), list(column.walk()))
        self.assertIn(column, list(tree.find("reviewPaneToolbar").ancestors()))

    def test_the_column_carries_no_per_finding_decision(self) -> None:
        out = self.run_studio(DOCX_ID, """
            docPlan = __input.plan;
            docPlanLoadedFor = docPlanSignature();
            renderReviewList();
            return { list: element('reviewGroups').innerHTML, toolbar: element('reviewPaneToolbar').innerHTML };
        """)["result"]
        listed = parse(out["list"])
        self.assertEqual([node for node in listed.walk() if "data-row-action" in node.attrs], [])
        self.assertEqual([node for node in listed.walk() if "data-box-action" in node.attrs], [])
        toolbar = [node.attrs["data-doc-action"] for node in parse(out["toolbar"]).walk() if "data-doc-action" in node.attrs]
        self.assertEqual(toolbar, ["approve", "dismiss"])

    def test_a_finding_with_no_bracket_is_listed_to_open_its_card(self) -> None:
        out = self.run_studio(DOCX_ID, """
            docPlan = { has_text: true, segments: [] };
            docPlanLoadedFor = docPlanSignature();
            renderReviewList();
            return element('reviewGroups').innerHTML;
        """)["result"]
        listed = sorted(int(node.attrs["data-open-finding"]) for node in parse(out).walk() if "data-open-finding" in node.attrs)
        self.assertEqual(listed, sorted(finding["id"] for finding in self.docs[DOCX_ID]["findings"]))

    def test_the_card_carries_every_finding_decision(self) -> None:
        finding = self.pending_docx_finding()
        out = self.run_studio(DOCX_ID, f"""
            openFindingPopover({finding}, {{ kind: 'list' }}, null);
            return element('assistantCurrentFinding').innerHTML;
        """)["result"]
        card = parse(out)
        actions = [node.attrs["data-row-action"] for node in card.walk() if "data-row-action" in node.attrs]
        self.assertEqual(actions, ["approve", "dismiss", "retain", "edit"])
        retain = next(node for node in card.walk() if node.attrs.get("data-row-action") == "retain")
        self.assertEqual(retain.text_content(), "Keep unredacted")
        self.assertEqual(card.find("assistantEditDetails").tag, "details")

    def test_a_pdf_findings_card_carries_its_boxes_as_their_own_decision(self) -> None:
        out = self.run_studio(PDF_ID, f"""
            openFindingPopover({self.linked_finding}, {{ kind: 'pdf', regionId: {self.linked_region} }}, null);
            return element('assistantCurrentFinding').innerHTML;
        """)["result"]
        card = parse(out)
        boxes = [(node.attrs["data-box-action"], node.attrs["data-region-ids"]) for node in card.walk() if "data-box-action" in node.attrs]
        self.assertEqual(boxes, [("approve", str(self.linked_region)), ("reject", str(self.linked_region))])
        self.assertIn("approve", [node.attrs.get("data-row-action") for node in card.walk()])

    def test_a_drawn_box_opens_a_card_carrying_the_box_decision(self) -> None:
        out = self.run_studio(PDF_ID, f"""
            openBoxPopover({self.manual_region});
            return element('assistantCurrentFinding').innerHTML;
        """)["result"]
        card = parse(out)
        boxes = [(node.attrs["data-box-action"], node.attrs["data-region-ids"]) for node in card.walk() if "data-box-action" in node.attrs]
        self.assertEqual(boxes, [("approve", str(self.manual_region)), ("reject", str(self.manual_region))])
        self.assertEqual([node for node in card.walk() if "data-row-action" in node.attrs], [])

    # 2. "Mark redaction complete" is on the document's status line.

    def test_the_status_slot_follows_the_document_status(self) -> None:
        tree = self.trees[DOCX_ID]
        slot = tree.find("docStatusAction")
        pill = tree.find("pipelinePill")
        self.assertIs(slot.parent, pill.parent)
        siblings = slot.parent.children
        self.assertLess(siblings.index(pill), siblings.index(slot))

    def test_mark_redaction_complete_is_on_the_status_line_and_not_in_the_step_bar(self) -> None:
        self.assertEqual(self.decided_docx["pending_finding_count"], 0)
        out = self.run_studio(DOCX_ID, """
            currentDoc = __input.doc;
            renderStepper(currentDoc);
            return {
                status: element('docStatusAction').innerHTML,
                stepper: element('workflowStepper').innerHTML,
                hint: element('workflowHint').innerHTML,
            };
        """, doc=self.decided_docx)["result"]
        buttons = [node for node in parse(out["status"]).walk() if node.tag == "button"]
        self.assertEqual([node.text_content() for node in buttons], ["Mark redaction complete"])
        self.assertEqual(buttons[0].attrs["onclick"], "stepAction('mark_redacted')")
        for bar in (out["stepper"], out["hint"]):
            labels = [node.text_content() for node in parse(bar).walk() if node.tag == "button"]
            self.assertNotIn("Mark redaction complete", labels)

    def test_the_status_line_carries_nothing_while_findings_are_pending(self) -> None:
        out = self.run_studio(DOCX_ID, """
            renderStepper(currentDoc);
            return element('docStatusAction').innerHTML;
        """)["result"]
        self.assertEqual(out.strip(), "")

    # 3. Navigation and failure land where a reviewer can see them.

    def test_the_findings_navigation_lands_in_the_review_column(self) -> None:
        log = self.run_studio(DOCX_ID, "navigateStudioSection('findings');")["log"]
        self.assertEqual(len(log["scrolled"]), 1)
        target = log["scrolled"][0]
        tree = self.trees[DOCX_ID]
        self.assertFalse(hidden_in(tree, target), target)
        column = next(node for node in tree.find("reviewPane").ancestors() if node.tag == "aside")
        self.assertIn(column, list(tree.find(target).ancestors()))
        self.assertEqual(log["focused"], [target])

    def test_a_load_failure_is_written_where_it_is_seen(self) -> None:
        out = self.run_studio(DOCX_ID, """
            renderWorkspaceFailure('Document could not be loaded.');
            return ['docPaper', 'reviewGroups', 'assistantCurrentFinding', 'matterDocumentList']
                .filter((id) => element(id).innerHTML.includes('Review context is unavailable'));
        """)["result"]
        self.assertEqual(len(out), 1)
        self.assertFalse(hidden_in(self.trees[DOCX_ID], out[0]), out[0])

    # 4. A shortcut decides only what is on screen.

    def test_a_shortcut_decides_the_finding_whose_card_is_on_screen(self) -> None:
        finding = self.pending_docx_finding()
        fetches = self.run_studio(DOCX_ID, self.docx_card_scenario(finding, drawn=True, keys="a"))["log"]["fetches"]
        self.assertEqual(fetches, [{"url": f"/api/finding/{finding}/review", "body": {"action": "approve"}}])

    def test_a_shortcut_does_not_decide_a_card_that_is_out_of_sight(self) -> None:
        """Open but not drawn: its bracket is scrolled away, or the redacted
        preview is hiding the document."""
        finding = self.pending_docx_finding()
        fetches = self.run_studio(DOCX_ID, self.docx_card_scenario(finding, drawn=False, keys="arR"))["log"]["fetches"]
        self.assertEqual(fetches, [])

    def test_a_shortcut_decides_the_card_not_a_selection_that_moved_away_from_it(self) -> None:
        """Dragging another finding's box on a PDF moves the selection and
        leaves the card where it was."""
        shown, selected = self.card_and_other_docx_finding()
        scenario = self.docx_card_scenario(shown, drawn=True, keys="") + f"selectedFindingId = {selected}; press('r');"
        fetches = self.run_studio(DOCX_ID, scenario)["log"]["fetches"]
        self.assertEqual(fetches, [{"url": f"/api/finding/{shown}/review", "body": {"action": "dismiss"}}])

    def test_save_edit_writes_onto_the_finding_the_card_shows(self) -> None:
        shown, selected = self.card_and_other_docx_finding()
        scenario = self.docx_card_scenario(shown, drawn=True, keys="") + f"""
            selectedFindingId = {selected};
            element('assistantReplacement').value = '[PARTY]';
            element('assistantReviewerNote').value = 'synthetic note';
            updateCurrentFinding();
        """
        fetches = self.run_studio(DOCX_ID, scenario)["log"]["fetches"]
        self.assertEqual([fetch["url"] for fetch in fetches], [f"/api/finding/{shown}/review"])
        self.assertEqual(fetches[0]["body"]["replacement_text"], "[PARTY]")

    def test_save_edit_with_no_card_writes_nothing(self) -> None:
        fetches = self.run_studio(DOCX_ID, f"""
            selectedFindingId = {self.pending_docx_finding()};
            updateCurrentFinding();
        """)["log"]["fetches"]
        self.assertEqual(fetches, [])

    def test_the_bulk_dismiss_warning_names_no_control_the_page_lacks(self) -> None:
        out = self.run_studio(DOCX_ID, """
            let message = '';
            window.confirm = (text) => { message = text; return false; };
            confirmFindingDecision('dismiss', 3, 'finding(s)', 'HIGH', true);
            docPlan = __input.plan;
            docPlanLoadedFor = docPlanSignature();
            renderReviewList();
            return { message, toolbar: element('reviewPaneToolbar').innerHTML };
        """)["result"]
        offered = {node.text_content().strip() for node in parse(out["toolbar"]).walk() if node.tag == "button"}
        self.assertNotIn("Keep all unredacted", out["message"])
        self.assertNotIn("Keep all unredacted", offered)
        self.assertIn("from its card", out["message"])

    def test_with_no_card_a_shortcut_never_decides_a_box_covering_a_finding(self) -> None:
        log = self.run_studio(PDF_ID, f"""
            pdfSelectedRegionId = {self.linked_region};
            ['r', 'R', 'Delete', 'Backspace', 'a', 'A'].forEach(press);
        """)["log"]
        self.assertEqual(log["fetches"], [])

    def test_with_no_card_a_shortcut_still_decides_a_drawn_box(self) -> None:
        for key, action in (("a", "approve"), ("Delete", "reject")):
            with self.subTest(key=key):
                log = self.run_studio(PDF_ID, f"""
                    pdfSelectedRegionId = {self.manual_region};
                    press({json.dumps(key)});
                """)["log"]
                self.assertEqual(
                    [fetch["url"] for fetch in log["fetches"]],
                    [f"/api/document/{PDF_ID}/pdf/regions/{self.manual_region}/{action}"],
                )


if __name__ == "__main__":
    unittest.main()
