// Replays every case gen_demo.py rendered with the product's redaction plan
// (legal_analyzer.redaction_plan.plan_segments) through the page's port,
// src/plan.js, and fails on the first difference in any segment's kind, span or
// attributed finding. Needs Node only; run from anywhere:
//
//     node landing/scripts/verify_plan.mjs
import { readFileSync } from "node:fs";
import { planSegments } from "../src/plan.js";

const demo = JSON.parse(readFileSync(new URL("../src/demo.json", import.meta.url)));
const { text, cases } = JSON.parse(readFileSync(new URL("./plan_cases.json", import.meta.url)));
if (text !== demo.text) throw new Error("plan_cases.json and demo.json were generated from different text");

const DECISIONS = { a: "approved", r: "retained", d: "dismissed", p: "pending" };
const KIND = { text: "t", marked: "m", redacted: "r" };
const placeholderOf = Object.fromEntries(demo.findings.map((f) => [f.id, f.placeholder]));

let failures = 0;
cases.forEach((c, i) => {
  if (c.decisions.length !== demo.findings.length) throw new Error(`case ${i}: one decision per finding expected`);
  const statuses = Object.fromEntries(demo.findings.map((f, j) => [f.id, DECISIONS[c.decisions[j]]]));
  const segments = planSegments(text, demo.findings, statuses);
  const got = segments.map((s) => [KIND[s.kind], s.start, s.end, s.findingId]);
  // The rendered run: a placeholder for a redacted run, the source characters otherwise.
  const textOk = segments.every((s) =>
    s.kind === "redacted" ? s.text === placeholderOf[s.findingId] : s.text === text.slice(s.start, s.end),
  );
  if ((JSON.stringify(got) !== JSON.stringify(c.segments) || !textOk) && failures++ < 3) {
    console.error(`case ${i} (${c.decisions}) differs`);
    console.error(" product:", JSON.stringify(c.segments).slice(0, 400));
    console.error(" port:   ", JSON.stringify(got).slice(0, 400));
  }
});
if (failures) {
  console.error(`verify_plan: ${failures} of ${cases.length} cases differ from the product`);
  process.exit(1);
}
console.log(`verify_plan: ${cases.length} of ${cases.length} cases match the product's redaction plan`);
