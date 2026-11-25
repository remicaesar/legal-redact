// The post-decision rendering the demo draws in its Export step.
//
// A line-for-line port of legal_analyzer.redaction_plan.plan_segments and
// docx_redactor.resolve_redaction, for this one document. The part that needs
// the product's Turkish case folding -- where each approved sample matches in
// the text, widened to word bounds -- is not ported: gen_demo.py computes it
// with the product's own find_case_insensitive and expand_to_word_bounds and
// ships it as `finding.matches`, the finding's own position flagged. What runs
// here is only the selection and gap-filling, and scripts/verify_plan.mjs
// replays 403 cases the product rendered through this file and fails on any
// difference.

export const REDACTED = new Set(["approved", "added_by_reviewer"]);

const statusOf = (statuses, f) => statuses[f.id] || "pending";

// resolve_redaction: sorted, disjoint [start, end, replacement, targetIndex].
// A backstop match (not the finding's own position) wholly inside a retained
// finding is left to that finding.
function resolveRegions(targets, retained) {
  const matches = [];
  targets.forEach((t, index) => {
    for (const [start, end, rawWidth, own] of t.matches) {
      if (!own && retained.some((r) => r.start <= start && end <= r.end)) continue;
      matches.push([start, end, rawWidth, own ? 0 : 1, t.placeholder, index]);
    }
  });
  // (start, then widest, then widest before widening, then own position, then earliest target)
  matches.sort((a, b) => a[0] - b[0] || (b[1] - b[0]) - (a[1] - a[0]) || b[2] - a[2] || a[3] - b[3] || a[5] - b[5]);
  const selected = [];
  let cursor = 0;
  for (const [start, end, , , replacement, index] of matches) {
    if (end <= cursor) continue;
    selected.push([Math.max(start, cursor), end, replacement, index]);
    cursor = end;
  }
  return selected;
}

function fillGap(segments, text, gapStart, gapEnd, marks) {
  if (gapEnd <= gapStart) return;
  const clipped = marks
    .filter((m) => m.start < gapEnd && gapStart < m.end)
    .map((m) => ({ start: Math.max(m.start, gapStart), end: Math.min(m.end, gapEnd), f: m.f }))
    .sort((a, b) => a.start - b.start || (b.end - b.start) - (a.end - a.start));
  let cursor = gapStart;
  for (let { start, end, f } of clipped) {
    if (end <= cursor) continue;
    start = Math.max(start, cursor);
    if (start > cursor) segments.push(plain(text, cursor, start));
    segments.push({ kind: "marked", start, end, text: text.slice(start, end), findingId: f.id, finding: f });
    cursor = end;
  }
  if (cursor < gapEnd) segments.push(plain(text, cursor, gapEnd));
}

const plain = (text, start, end) => ({ kind: "text", start, end, text: text.slice(start, end), findingId: null });

// plan_segments: every run of the document under the given decisions.
export function planSegments(text, findings, statuses) {
  const targets = findings.filter((f) => f.sample && REDACTED.has(statusOf(statuses, f)));
  const retained = findings.filter((f) => statusOf(statuses, f) === "retained");
  const regions = resolveRegions(targets, retained);
  const marks = findings
    .filter((f) => !REDACTED.has(statusOf(statuses, f)))
    .map((f) => ({ start: f.start, end: f.end, f }));
  const segments = [];
  let cursor = 0;
  for (const [start, end, replacement, index] of regions) {
    fillGap(segments, text, cursor, start, marks);
    const t = targets[index];
    segments.push({
      kind: "redacted",
      start,
      end,
      text: replacement,
      source: text.slice(start, end),
      findingId: t.id,
      finding: t,
    });
    cursor = end;
  }
  fillGap(segments, text, cursor, text.length, marks);
  return segments;
}

export const planText = (text, findings, statuses) =>
  planSegments(text, findings, statuses).map((s) => s.text).join("");

// Before any decision is rendered: the source text with each finding as a mark,
// at the offsets detection recorded. The findings in this document are disjoint.
export function sourceSegments(text, findings) {
  const segments = [];
  let cursor = 0;
  for (const f of [...findings].sort((a, b) => a.start - b.start)) {
    if (f.start < cursor) continue;
    if (f.start > cursor) segments.push(plain(text, cursor, f.start));
    segments.push({ kind: "marked", start: f.start, end: f.end, text: text.slice(f.start, f.end), findingId: f.id, finding: f });
    cursor = f.end;
  }
  if (cursor < text.length) segments.push(plain(text, cursor, text.length));
  return segments;
}

// Split a run list into paragraphs at the newlines the extraction put between
// DOCX paragraphs. No finding contains a newline (tests/test_redaction_parity.py).
export function toParagraphs(segments) {
  const paras = [[]];
  for (const seg of segments) {
    if (seg.kind !== "text") {
      paras[paras.length - 1].push(seg);
      continue;
    }
    const parts = seg.text.split("\n");
    let offset = seg.start;
    parts.forEach((part, i) => {
      if (i > 0) paras.push([]);
      if (part) paras[paras.length - 1].push({ ...seg, start: offset, end: offset + part.length, text: part });
      offset += part.length + 1;
    });
  }
  return paras.filter((p) => p.some((s) => s.text.trim()));
}
