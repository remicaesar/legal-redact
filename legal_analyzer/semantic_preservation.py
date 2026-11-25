"""Score a sanitized document on two dimensions, not one.

PII precision and recall say whether identifiers were FOUND. They cannot see
the failure this module exists for: a pass that removes every identifier by
deleting the clauses around them scores perfectly and leaves a document no
lawyer -- and no model -- can analyse. The synthetic NDA was exactly that: its
confidentiality definition, exceptions and obligations were replaced by
[PRIVILEGED_CONTENT_n_REDACTED] while every identifier was correctly caught.

So a sanitized document is scored twice, on the rendering the product would
actually export:

* **privacy protection** -- of the identifiers that must not leave the
  workstation, how many are gone from the rendering;
* **legal semantic preservation** -- of the legally operative terms in the
  source (``legal_semantics.legal_terms``: the penalty clause, the
  confidentiality definition and its exceptions, durations, survival, governing
  law, jurisdiction, obligations, parties' roles, ...), how many still read in
  the rendering.

A document is poor if either number is poor. The rendering is the product's
own post-decision text (``redaction_plan.plan_segments``) under a stated set of
decisions -- by default the worst case, every finding approved, which is what
the Studio's "approve all" produces and what deleted the NDA's clauses.

Term survival is a COUNT per term (its kind and its folded text), so a term the
source uses three times must still appear three times: a clause deleted once
out of three is visible. It is lexical, and says so -- it measures whether the
operative words are still there, not whether a reader would draw the right
conclusion from them.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass

from legal_analyzer import legal_semantics
from legal_analyzer.privacy import REDACTED_REVIEW_STATUSES
from legal_analyzer.redaction_plan import plan_segments
from legal_analyzer.redaction_policy import replacement_for_finding


def _plan_rows(findings: list[dict], decisions: dict[int, str] | None) -> list[dict]:
    """analyze_privacy findings in the shape redaction_plan reads, decided.

    ``decisions`` maps a finding's index to a review status; a finding with no
    entry is approved.
    """
    rows = []
    for index, finding in enumerate(findings):
        rows.append(
            {
                "id": index,
                "category": finding["category"],
                "sample": finding["sample"],
                "placeholder": finding.get("placeholder"),
                "replacement_text": replacement_for_finding(finding),
                "review_status": (decisions or {}).get(index, "approved"),
                "source": finding.get("source", "detector"),
                "start_offset": finding.get("start"),
                "end_offset": finding.get("end"),
            }
        )
    return rows


@dataclass(frozen=True)
class Rendering:
    text: str
    redacted_spans: tuple[tuple[int, int, str | None], ...]  # (start, end, category) in the SOURCE


def render(text: str, findings: list[dict], decisions: dict[int, str] | None = None) -> Rendering:
    """The text the reviewed export would carry under ``decisions``."""
    segments = plan_segments(text, _plan_rows(findings, decisions), REDACTED_REVIEW_STATUSES)
    return Rendering(
        text="".join(segment.text for segment in segments),
        redacted_spans=tuple(
            (segment.start, segment.end, segment.category) for segment in segments if segment.kind == "redacted"
        ),
    )


def _covered(spans: tuple[tuple[int, int, str | None], ...]) -> list[tuple[int, int]]:
    return sorted((start, end) for start, end, _category in spans)


def entity_removed(rendering: Rendering, start: int, end: int) -> bool:
    """Every character of the source span ``[start, end)`` is inside a redacted region."""
    cursor = start
    for span_start, span_end in _covered(rendering.redacted_spans):
        if span_end <= cursor:
            continue
        if span_start > cursor:
            return False
        cursor = span_end
        if cursor >= end:
            return True
    return cursor >= end


def term_counts(text: str) -> collections.Counter:
    """Operative terms of ``text``, counted by (provision kind, folded term text)."""
    return collections.Counter((term.kind, legal_semantics.fold(term.text)) for term in legal_semantics.legal_terms(text))


@dataclass
class SemanticScore:
    terms: int = 0
    kept: int = 0
    by_kind: dict = None

    @property
    def share(self) -> float | None:
        return round(self.kept / self.terms, 3) if self.terms else None

    def as_dict(self) -> dict:
        return {"terms": self.terms, "kept": self.kept, "share": self.share, "by_kind": self.by_kind or {}}


def legal_semantic_preservation(source: str, rendered: str) -> SemanticScore:
    """How many of the source's operative-term occurrences still read in ``rendered``."""
    before = term_counts(source)
    after = term_counts(rendered)
    by_kind: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    for key, count in before.items():
        by_kind[key[0]][0] += count
        by_kind[key[0]][1] += min(count, after.get(key, 0))
    return SemanticScore(
        terms=sum(before.values()),
        kept=sum(min(count, after.get(key, 0)) for key, count in before.items()),
        by_kind={kind: {"terms": total, "kept": kept} for kind, (total, kept) in sorted(by_kind.items())},
    )


def passage_characters_removed(rendering: Rendering, passage_categories: frozenset[str] | set[str]) -> int:
    """Source characters removed as whole passages by ``passage_categories`` findings.

    The clause-level context rules (health data, criminal allegations,
    privilege) remove a passage rather than a value. Characters they remove are
    the ones a downstream reader loses wholesale, so they are reported apart
    from the characters identifier findings replace.
    """
    return sum(end - start for start, end, category in rendering.redacted_spans if category in passage_categories)
