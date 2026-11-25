"""The one post-decision rendering of a document, shared by every surface.

There used to be three answers to "what will the redacted file look like", and
they disagreed on the same document with the same review decisions:

- the browser review canvas derived its own rendering in JavaScript, replacing
  each finding's recorded OFFSETS and letting the NARROWEST span win an overlap;
- the "Preview TXT / Preview DOCX" server preview shipped the DETECTION-TIME
  ``redacted_preview``, which replaced offsets too, resolved overlaps the other
  way round, and ignored the reviewer's decisions entirely;
- the exported reviewed DOCX -- the actual deliverable -- replaced every STRING
  match of each approved sample.

Measured on one synthetic Turkish petition: 5 differing hunks over 27 lines
between the preview and the export, and all three pairwise combinations
disagreeing within the first 12 lines. The lawyer approved against a rendering
that was not the deliverable and had no way to tell.

Two remain. The preview export was deleted rather than kept in agreement: a
rebuilt, non-layout-preserving ``<name>_redacted.txt`` headed
``PRIVACY-REVIEWED REDACTED EXPORT`` reads as the deliverable and is not one,
and before any review decision its body was the unredacted source. Reverting it
to the detection-time snapshot would have restored the opposite lie -- claiming
a dismissed finding was removed -- so there was no honest version to keep.

This module is the single answer. ``plan_segments`` resolves the post-decision
text ONCE, server-side, through ``docx_redactor.resolve_redaction`` -- the same
function ``redact_docx`` uses, handed the same inputs (``redaction_inputs``:
each approved finding anchored at its own position, plus the findings the
reviewer retained) -- and every surface renders what it returns. The canvas no
longer decides anything; it draws segments.

``tests/test_app_workflow.py`` pins the address-context boundary result on both
of the surviving surfaces through this module, before and after review, so a
regression in the boundary is caught where the reviewer would meet it rather
than only in the detector's own output.

``build_redacted_preview`` in ``privacy.py`` is deliberately untouched. It is the
DETECTION-time measurement the accuracy audit and the stored privacy profile are
calibrated on, in the same way ``residual_risk`` is, and it answers a different
question: what did the detector find, before anyone reviewed it.
"""

from __future__ import annotations

from dataclasses import dataclass

from legal_analyzer.docx_redactor import (
    Occurrence,
    RedactionTarget,
    RetainedSpan,
    is_standalone_sample,
    resolve_redaction,
)
from legal_analyzer.privacy import finding_needles

__all__ = [
    "PlanTarget",
    "PlanSegment",
    "RetainedHold",
    "held_by_retained",
    "plan_segments",
    "plan_text",
    "redaction_inputs",
    "targets_from",
]

RETAINED_STATUS = "retained"


@dataclass(frozen=True)
class PlanTarget:
    """An approved finding, as the resolver sees it plus what it came from."""

    text: str
    replacement: str
    finding_id: int | None = None
    category: str | None = None
    review_status: str | None = None
    source: str | None = None
    start_offset: int | None = None


@dataclass(frozen=True)
class RetainedHold:
    """An approved finding's text that a retained finding keeps in the document.

    The reviewed-DOCX export refuses while any exists (``app.docx_export_conditions``),
    and says which two decisions disagree.
    """

    finding_id: int | None
    sample: str
    category: str | None
    retained_finding_id: int | None
    retained_sample: str
    retained_category: str | None


@dataclass(frozen=True)
class PlanSegment:
    """One run of the document, already resolved.

    ``text`` is what every surface renders for this run -- a placeholder for a
    redacted run, the original characters otherwise. ``source_text`` is what the
    run covers in the source, which the canvas needs for its title tooltips and
    nothing else renders.
    """

    kind: str  # "text" | "redacted" | "marked"
    text: str
    source_text: str
    start: int
    end: int
    finding_id: int | None = None
    finding_ids: tuple[int, ...] = ()
    category: str | None = None
    review_status: str | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "text": self.text,
            "source_text": self.source_text,
            "start": self.start,
            "end": self.end,
            "finding_id": self.finding_id,
            "finding_ids": list(self.finding_ids),
            "category": self.category,
            "review_status": self.review_status,
        }


def targets_from(findings: list[dict], redacted_statuses: frozenset[str] | set[str]) -> list[PlanTarget]:
    """Approved findings, in the order and shape the resolver expects.

    Mirrors ``app.redaction_targets_for_document`` -- same statuses, same
    replacement precedence -- so the plan and the DOCX export cannot be handed
    different target sets. The rows here carry the finding id as well, which the
    export path has no use for and the canvas needs to stay clickable.
    """
    targets: list[PlanTarget] = []
    for finding in findings:
        sample = finding.get("sample") or ""
        if not sample:
            continue
        if (finding.get("review_status") or "pending") not in redacted_statuses:
            continue
        for index, needle in enumerate(finding_needles(finding.get("category"), sample)):
            targets.append(
                PlanTarget(
                    text=needle,
                    replacement=finding.get("replacement_text") or finding.get("placeholder") or "[REDACTED]",
                    finding_id=finding.get("id"),
                    category=finding.get("category"),
                    review_status=finding.get("review_status"),
                    source=finding.get("source"),
                    start_offset=finding.get("start_offset") if index == 0 else None,
                )
            )
    return targets


def _retained_findings(text: str, findings: list[dict]) -> list[tuple[RetainedSpan, dict]]:
    """The retained findings whose recorded offset still frames their sample in ``text``.

    One whose offset is stale holds nothing back: it cannot be placed, and the
    backstop then redacts its text the way it always did.
    """
    spans: list[tuple[RetainedSpan, dict]] = []
    for finding in findings:
        if (finding.get("review_status") or "pending") != RETAINED_STATUS:
            continue
        sample = finding.get("sample") or ""
        found_at = Occurrence.at(text, sample, finding.get("start_offset"))
        if found_at is not None:
            spans.append((RetainedSpan(sample, found_at), finding))
    return spans


def redaction_inputs(
    text: str,
    findings: list[dict],
    redacted_statuses: frozenset[str] | set[str],
) -> tuple[list[PlanTarget], list[RedactionTarget], list[tuple[RetainedSpan, dict]]]:
    """What the shared resolver is handed for ``text``, by the canvas and the export alike.

    A finding's own position is anchored (``Occurrence``) only where its
    recorded offset still frames its sample in ``text`` -- the extracted text
    detection read. ``app.redaction_targets_for_document`` builds the reviewed
    DOCX export's targets through this function, so the canvas and the export
    cannot be handed different anchors.
    """
    plan_targets = targets_from(findings, redacted_statuses)
    targets = [
        RedactionTarget(
            target.text,
            target.replacement,
            is_standalone_sample(target.category, target.text, target.source),
            Occurrence.at(text, target.text, target.start_offset),
        )
        for target in plan_targets
    ]
    return plan_targets, targets, _retained_findings(text, findings)


def held_by_retained(
    text: str,
    findings: list[dict],
    redacted_statuses: frozenset[str] | set[str],
) -> list[RetainedHold]:
    """Every approved finding whose text a retained finding keeps in ``text``, once per pair."""
    plan_targets, targets, retained = redaction_inputs(text, findings, redacted_statuses)
    resolution = resolve_redaction(text, targets, retained=[span for span, _ in retained])
    holds: list[RetainedHold] = []
    for match in resolution.held:
        target = plan_targets[match.target_index]
        keeper = retained[match.retained_index][1]
        hold = RetainedHold(
            finding_id=target.finding_id,
            sample=target.text,
            category=target.category,
            retained_finding_id=keeper.get("id"),
            retained_sample=keeper.get("sample") or "",
            retained_category=keeper.get("category"),
        )
        if hold not in holds:
            holds.append(hold)
    return holds


def _resolved_finding_spans(text: str, findings: list[dict]) -> list[tuple[int, int, dict]]:
    """Where each finding sits in ``text``, for attribution and for marking.

    A recorded offset is trusted only when it still frames its own sample.
    Offsets belong to the extraction that detection ran on; a re-extraction, an
    accepted OCR page or a reviewer-added finding can leave them stale, and a
    stale offset that still points somewhere would mark the wrong words. The
    sample is then located by search instead -- the same fallback the canvas
    already applied, kept because it is right, and moved server-side with the
    rest of the decision.
    """
    resolved: list[tuple[int, int, dict]] = []
    for finding in findings:
        sample = finding.get("sample") or ""
        start = finding.get("start_offset")
        end = finding.get("end_offset")
        if (
            isinstance(start, int)
            and isinstance(end, int)
            and 0 <= start < end <= len(text)
            and (not sample or text[start:end] == sample)
        ):
            resolved.append((start, end, finding))
            continue
        if not sample:
            continue
        index = text.find(sample)
        if index == -1:
            continue
        resolved.append((index, index + len(sample), finding))
    return resolved


def plan_segments(
    text: str,
    findings: list[dict],
    redacted_statuses: frozenset[str] | set[str],
    style: str = "placeholder",
) -> list[PlanSegment]:
    """Resolve ``text`` under the current review decisions, once, for everyone.

    Approved findings are resolved first, by the shared resolver -- each at its
    own position, and its text elsewhere by the backstop: they define the
    deliverable, so nothing may take a character away from them.
    Every other finding is then laid into the gaps left over, clipped, so the
    canvas keeps a clickable span for a pending or dismissed finding without
    that span ever contradicting what the export will do. Where a pending
    finding sits wholly inside an approved region there is no gap and no marked
    segment -- correctly: those characters are leaving the document, and the
    finding is still reachable from the findings list.

    Gap ordering is (start, then widest), the same tie-break the resolver uses,
    so a marked span never depends on which surface is drawing it. Text a
    retained finding holds back from the backstop (``resolve_redaction``) is
    left to that finding, so it is drawn as the retained finding's mark.
    """
    targets, resolver_targets, retained = redaction_inputs(text, findings, redacted_statuses)
    regions = resolve_redaction(text, resolver_targets, style, [span for span, _ in retained]).regions
    spans = _resolved_finding_spans(text, findings)
    overlapping_ids = _overlap_index(spans)

    marks = [
        (start, end, finding)
        for start, end, finding in spans
        if (finding.get("review_status") or "pending") not in redacted_statuses
    ]

    segments: list[PlanSegment] = []
    cursor = 0
    for start, end, replacement, target_index in regions:
        _fill_gap(segments, text, cursor, start, marks, overlapping_ids)
        target = targets[target_index]
        segments.append(
            PlanSegment(
                kind="redacted",
                text=replacement,
                source_text=text[start:end],
                start=start,
                end=end,
                finding_id=target.finding_id,
                finding_ids=overlapping_ids(start, end),
                category=target.category,
                review_status=target.review_status,
            )
        )
        cursor = end
    _fill_gap(segments, text, cursor, len(text), marks, overlapping_ids)
    return segments


def _overlap_index(spans: list[tuple[int, int, dict]]):
    def ids_for(start: int, end: int) -> tuple[int, ...]:
        return tuple(
            finding["id"]
            for span_start, span_end, finding in spans
            if finding.get("id") is not None and span_start < end and start < span_end
        )

    return ids_for


def _fill_gap(
    segments: list[PlanSegment],
    text: str,
    gap_start: int,
    gap_end: int,
    marks: list[tuple[int, int, dict]],
    overlapping_ids,
) -> None:
    if gap_end <= gap_start:
        return
    clipped = sorted(
        (
            (max(start, gap_start), min(end, gap_end), finding)
            for start, end, finding in marks
            if start < gap_end and gap_start < end
        ),
        key=lambda item: (item[0], -(item[1] - item[0])),
    )
    cursor = gap_start
    for start, end, finding in clipped:
        if end <= cursor:
            continue
        start = max(start, cursor)
        if start > cursor:
            segments.append(_plain(text, cursor, start))
        segments.append(
            PlanSegment(
                kind="marked",
                text=text[start:end],
                source_text=text[start:end],
                start=start,
                end=end,
                finding_id=finding.get("id"),
                finding_ids=overlapping_ids(start, end),
                category=finding.get("category"),
                review_status=finding.get("review_status") or "pending",
            )
        )
        cursor = end
    if cursor < gap_end:
        segments.append(_plain(text, cursor, gap_end))


def _plain(text: str, start: int, end: int) -> PlanSegment:
    return PlanSegment(kind="text", text=text[start:end], source_text=text[start:end], start=start, end=end)


def plan_text(
    text: str,
    findings: list[dict],
    redacted_statuses: frozenset[str] | set[str],
    style: str = "placeholder",
) -> str:
    """The post-decision text: what the reviewed export will read."""
    return "".join(segment.text for segment in plan_segments(text, findings, redacted_statuses, style))
