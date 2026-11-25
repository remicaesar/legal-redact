"""Coordinate-based PDF redaction helpers."""

from __future__ import annotations

import base64
import codecs
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote

import pymupdf  # type: ignore

# The matcher lives in docx_redactor because that is where it was first needed;
# it is format-agnostic. Sharing it is the point: a PDF QA pass that searched for
# approved text with a plain `in` reported zero leaks for exactly the case and
# diacritic variants the DOCX side already treats as leaks.
from legal_analyzer.docx_redactor import (
    FoldedText,
    RedactionTarget,
    SampleMatcher,
    collect_replacements,
    contains_case_insensitive,
    find_case_insensitive,
    is_standalone_sample,
    purge_target_pattern_cache,
    sample_match,
)
from legal_analyzer.export_scan import output_scan_check
from legal_analyzer.privacy import finding_needles


@dataclass(frozen=True)
class PdfRegion:
    page_number: int
    x0: float
    y0: float
    x1: float
    y1: float
    category: str = "manual_sensitive_text"
    source: str = "manual"
    finding_id: int | None = None
    sample: str | None = None
    region_id: int | None = None
    # "finding" for the box at the finding's own occurrence, "repeat" for any
    # other box of its text, None where the two could not be told apart.
    occurrence: str | None = None


def clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def normalized_region(page_number: int, rect: pymupdf.Rect, page_rect: pymupdf.Rect, **kwargs) -> PdfRegion:
    width = page_rect.width or 1
    height = page_rect.height or 1
    return PdfRegion(
        page_number=page_number,
        x0=clamp(rect.x0 / width),
        y0=clamp(rect.y0 / height),
        x1=clamp(rect.x1 / width),
        y1=clamp(rect.y1 / height),
        **kwargs,
    )


def denormalized_rect(region: PdfRegion, page_rect: pymupdf.Rect) -> pymupdf.Rect:
    x0 = min(region.x0, region.x1) * page_rect.width
    x1 = max(region.x0, region.x1) * page_rect.width
    y0 = min(region.y0, region.y1) * page_rect.height
    y1 = max(region.y0, region.y1) * page_rect.height
    return pymupdf.Rect(x0, y0, x1, y1)


def find_pdf_regions(path: Path, findings: list[dict], text: str | None = None) -> list[PdfRegion]:
    """A suggested box over every match of each finding's sample, on every page.

    Every match, not only the finding's own: this is the PDF's backstop, and a
    repeat detection missed gets a box like the rest. Unlike the DOCX export, no
    box is redacted on the strength of the finding alone -- each is approved or
    rejected on its own (``pdf_export_blockers`` refuses on an unreviewed one).

    Given the extracted ``text`` the finding's ``start_offset`` was recorded
    against, each box is labelled ``occurrence="finding"`` at the finding's own
    occurrence and ``"repeat"`` elsewhere -- but only where the pairing is
    provable (``_occurrence_labels``). Otherwise, or without ``text``, the boxes
    carry no label rather than a guessed one.
    """
    regions: list[PdfRegion] = []
    page_lines: dict[int, list[_PageLine]] = {}
    with pymupdf.open(path) as doc:
        for finding in findings:
            boxed: list[tuple[int, pymupdf.Rect]] = []
            for index, needle in enumerate(finding_needles(finding.get("category"), (finding.get("sample") or "").strip())):
                sample = needle.strip()
                standalone = is_standalone_sample(finding.get("category"), sample, finding.get("source"))
                hits: list[tuple[int, pymupdf.Rect, pymupdf.Rect]] = []
                for page_index, page in enumerate(doc, start=1):
                    rects = page.search_for(sample)
                    if standalone:
                        words = [
                            pymupdf.Rect(word[:4])
                            for word in page.get_text("words")
                            if contains_case_insensitive(word[4], sample, standalone=True)
                        ]
                        rects = [
                            rect
                            for rect in rects
                            if any(word.contains((rect.tl + rect.br) / 2) for word in words)
                        ]
                    rects = [
                        rect
                        for rect in rects
                        if not any(page_index == box_page and box.contains((rect.tl + rect.br) / 2) for box_page, box in boxed)
                    ]
                    hits.extend((page_index, page.rect, rect) for rect in rects)
                boxed.extend((page_index, rect) for page_index, _, rect in hits)
                start_offset = finding.get("start_offset") if index == 0 else None
                labels = _occurrence_labels(doc, page_lines, hits, text, sample, standalone, start_offset)
                for (page_index, page_rect, rect), label in zip(hits, labels):
                    regions.append(
                        normalized_region(
                            page_index,
                            rect,
                            page_rect,
                            category=finding.get("category") or "sensitive_text",
                            source="ocr" if finding.get("source") == "ocr" else "detected_text",
                            finding_id=finding.get("id"),
                            sample=sample,
                            occurrence=label,
                        )
                    )
    purge_target_pattern_cache()
    return regions


def _line_text(text: str) -> str:
    return " ".join(text.split())


def _line_around(text: str, begin: int, end: int) -> str:
    line_end = text.find("\n", end)
    return _line_text(text[text.rfind("\n", 0, begin) + 1 : len(text) if line_end == -1 else line_end])


# One line of a page as ``rawdict`` reads it: each character with the centre of
# its box, which is what decides whether a hit's rect holds it.
_PageLine = list[tuple[str, pymupdf.Point]]


def _read_page_lines(page: pymupdf.Page) -> list[_PageLine]:
    return [
        [
            (char["c"], (pymupdf.Rect(char["bbox"]).tl + pymupdf.Rect(char["bbox"]).br) / 2)
            for span in line.get("spans", [])
            for char in span.get("chars", [])
        ]
        for block in page.get_text("rawdict").get("blocks", [])
        for line in block.get("lines", [])
    ]


def _hit_text_and_line(lines: list[_PageLine], rect: pymupdf.Rect) -> tuple[str, str] | None:
    """The characters under ``rect`` and the text of the one line holding them, as the page reads."""
    found: list[tuple[str, str]] = []
    for chars in lines:
        inside = "".join(char for char, centre in chars if rect.contains(centre))
        if inside:
            found.append((inside, "".join(char for char, _ in chars)))
    return found[0] if len(found) == 1 else None


def _occurrence_labels(
    doc: pymupdf.Document,
    page_lines: dict[int, list[_PageLine]],
    hits: list[tuple[int, pymupdf.Rect, pymupdf.Rect]],
    text: str | None,
    sample: str,
    standalone: bool,
    start: int | None,
) -> list[str | None]:
    """``"finding"`` / ``"repeat"`` per hit, or ``None`` for every hit unless the pairing is provable.

    Provable means: the extracted text and the page hold the same matches of the
    sample under the export's own matcher (``find_case_insensitive``), every one
    of them spelled exactly as the sample on both sides, as many on each side;
    each hit's line reads as the line of the occurrence it is paired with; and
    the finding's own line is unlike every other occurrence's. A case or
    diacritic variant, a count mismatch, or two readings that order or break the
    lines differently (a two-column page) leave every box unlabelled.

    ``page_lines`` caches each page's lines for the whole ``find_pdf_regions``
    call: a name found N times is N findings of N hits each, and reading the
    page per hit parsed it N^2 times.
    """
    unlabelled: list[str | None] = [None] * len(hits)
    if text is None or not isinstance(start, int) or not hits:
        return unlabelled
    spans = find_case_insensitive(text, sample, standalone)
    if any(text[begin:end] != sample for begin, end in spans) or len(spans) != len(hits):
        return unlabelled
    starts = [begin for begin, _ in spans]
    if start not in starts:
        return unlabelled
    text_lines = [_line_around(text, begin, end) for begin, end in spans]
    hit_lines: list[str] = []
    for page_index, _, rect in hits:
        if page_index not in page_lines:
            page_lines[page_index] = _read_page_lines(doc[page_index - 1])
        read = _hit_text_and_line(page_lines[page_index], rect)
        if read is None or read[0] != sample:
            return unlabelled
        hit_lines.append(_line_text(read[1]))
    own = starts.index(start)
    if hit_lines != text_lines or text_lines.count(text_lines[own]) != 1:
        return unlabelled
    return ["finding" if index == own else "repeat" for index in range(len(hits))]


# What replaces an approved item in the text a PDF keeps outside its pages --
# outline titles and page-label prefixes. The page itself gets a black box; a
# string cannot, so it gets the box as glyphs. No letter or digit, so no sample
# can be found in it: "[REDACTED]" holds "Ted".
PDF_REDACTION_MARK = "█████"


def redact_document_text(doc: pymupdf.Document, targets: list[RedactionTarget]) -> None:
    """Redact the approved targets wherever a PDF carries text outside its pages' content.

    Outline (bookmark) titles and page-label prefixes are text a reader sees in
    the viewer's side panel and page box: the matched text is replaced with
    ``PDF_REDACTION_MARK``, or the whole string when a target still survives in
    it (``SampleMatcher``, the export scan's rule). A link whose target carries
    a target is removed: its URI is no text a reader sees, and the words on the
    page it sat over are blacked out with the rest of the page. Every embedded
    file is removed, whatever it holds: the review canvas never shows one, and
    nothing here can redact inside one.
    """
    for name in doc.embfile_names():
        doc.embfile_del(name)
    marked = [RedactionTarget(target.text, PDF_REDACTION_MARK, target.standalone) for target in targets if target.text]
    matchers = [SampleMatcher.compile(target.text, target.standalone) for target in marked]
    if not matchers:
        return

    def carries(text: str) -> bool:
        folded = FoldedText.of(text)
        return any(matcher.found_in(folded) for matcher in matchers)

    def redacted(text: str) -> str:
        for start, end, replacement in reversed(collect_replacements(text, marked)):
            text = text[:start] + replacement + text[end:]
        return PDF_REDACTION_MARK if carries(text) else text

    for _, title, _, destination in doc.get_toc(simple=False):
        if title != (replacement := redacted(title)):
            doc.xref_set_key(destination["xref"], "Title", pymupdf.get_pdf_str(replacement))
    labels = [{**rule, "prefix": _label_prefix(rule.get("prefix") or "")} for rule in doc.get_page_labels()]
    if any(rule["prefix"] != redacted(rule["prefix"]) for rule in labels):
        _set_page_labels(doc, [{**rule, "prefix": redacted(rule["prefix"])} for rule in labels])
    for page in doc:
        for link in page.get_links():
            values = [link[key] for key in ("uri", "file", "nameddest", "name") if isinstance(link.get(key), str)]
            if any(carries(value) or carries(unquote(value)) for value in values):
                page.delete_link(link)


def _label_prefix(raw: str) -> str:
    """A page-label prefix as text. PyMuPDF hands a hex string back undecoded ("<FEFF015E...>")."""
    hex_digits = re.fullmatch(r"<([0-9A-Fa-f\s]*)>", raw)
    if hex_digits is None:
        return raw
    digits = re.sub(r"\s", "", hex_digits.group(1))
    data = bytes.fromhex(digits + "0" * (len(digits) % 2))
    if data.startswith(codecs.BOM_UTF16_BE):
        return data[2:].decode("utf-16-be", errors="replace")
    return data.decode("utf-8-sig") if data.startswith(codecs.BOM_UTF8) else data.decode("latin-1")


def _set_page_labels(doc: pymupdf.Document, labels: list[dict]) -> None:
    """``Document.set_page_labels``, but with the prefix written as a PDF string.

    PyMuPDF writes the prefix between parentheses as it is, so any character
    outside PDFDocEncoding -- the redaction mark, a Turkish letter -- is stored
    as bytes a viewer reads as other characters.
    """
    nums = "".join(
        f"{rule['startpage']}<<"
        + (f"/P{pymupdf.get_pdf_str(rule['prefix'])}" if rule.get("prefix") else "")
        + (f"/S/{rule['style']}" if rule.get("style") else "")
        + (f"/St {rule['firstpagenum']}" if rule.get("firstpagenum", 1) > 1 else "")
        + ">>"
        for rule in labels
    )
    doc.xref_set_key(doc.pdf_catalog(), "PageLabels", f"<</Nums[{nums}]>>")


def redact_pdf(path: Path, regions: list[PdfRegion], targets: list[RedactionTarget] = ()) -> bytes:
    """The reviewed PDF: ``regions`` blacked out, then ``targets`` redacted outside the pages (``redact_document_text``)."""
    by_page: dict[int, list[PdfRegion]] = {}
    for region in regions:
        by_page.setdefault(region.page_number, []).append(region)
    with pymupdf.open(path) as doc:
        for page_number, page_regions in sorted(by_page.items()):
            if page_number < 1 or page_number > doc.page_count:
                continue
            page = doc[page_number - 1]
            applied = False
            for region in page_regions:
                rect = denormalized_rect(region, page.rect)
                if rect.is_empty or rect.get_area() <= 0:
                    continue
                page.add_redact_annot(rect, fill=(0, 0, 0))
                applied = True
            if applied:
                page.apply_redactions()
        for page in doc:
            annot = page.first_annot
            while annot:
                next_annot = annot.next
                page.delete_annot(annot)
                annot = next_annot
        redact_document_text(doc, targets)
        # Both metadata stores: the Info dictionary and the XMP packet. Clearing
        # only the first kept a client name that was in the XMP title.
        doc.set_metadata({})
        doc.del_xml_metadata()
        buffer = BytesIO()
        doc.save(buffer, garbage=4, deflate=True, clean=True)
        return buffer.getvalue()


def extract_pdf_text_from_bytes(data: bytes) -> str:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return "\n".join(page.get_text("text") for page in doc)


# Keys PyMuPDF reports in ``doc.metadata`` that are not stored metadata: the
# file's version and its encryption method. "format" is never empty, so a check
# that counted it warned on every PDF, leaking or not.
PDF_SYNTHETIC_METADATA_KEYS = frozenset({"format", "encryption"})


def pdf_metadata_leaks(data: bytes) -> list[str]:
    """Name the document metadata a PDF still carries: Info keys, and XMP.

    Names only, never values -- this feeds the export QA report.
    """
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        info = dict(doc.metadata or {})
        xmp = doc.get_xml_metadata() or ""
    leaks = sorted(key for key, value in info.items() if value and key not in PDF_SYNTHETIC_METADATA_KEYS)
    if xmp.strip():
        leaks.append("xmp")
    return leaks


def count_pdf_annotations(data: bytes) -> int:
    count = 0
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            annot = page.first_annot
            while annot:
                count += 1
                annot = annot.next
    return count


def render_page_thumbnail(doc: pymupdf.Document, page_number: int, width: int = 220) -> str:
    page = doc[page_number - 1]
    zoom = width / max(page.rect.width, 1)
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    return base64.b64encode(pixmap.tobytes("png")).decode("ascii")


def visual_qa_thumbnails(source_path: Path, data: bytes, regions: list[PdfRegion]) -> dict:
    page_number = min((region.page_number for region in regions), default=1)
    try:
        with pymupdf.open(source_path) as source_doc:
            if not 1 <= page_number <= source_doc.page_count:
                page_number = 1
            before = render_page_thumbnail(source_doc, page_number)
        with pymupdf.open(stream=data, filetype="pdf") as redacted_doc:
            after = render_page_thumbnail(redacted_doc, min(page_number, redacted_doc.page_count))
    except Exception as exc:  # rendering support can vary by build; QA must not crash on it
        return {
            "status": "skipped",
            "note": f"Visual QA thumbnails were skipped because local rendering failed: {type(exc).__name__}.",
        }
    return {
        "status": "rendered",
        "page_number": page_number,
        "before_png_base64": before,
        "after_png_base64": after,
        "note": "Thumbnails are review aids only; text-extraction checks above are the security-relevant QA.",
    }


def analyze_pdf_redaction_quality(
    source_path: Path,
    data: bytes,
    regions: list[PdfRegion],
    region_state: dict | None = None,
    qa_target: str = "freshly_generated",
    sensitive_samples: list[str | RedactionTarget] | None = None,
    retained_count: int = 0,
    output_leaks: list[dict] | None = None,
) -> dict:
    """Check a redacted PDF for extractable approved text, metadata and annotations.

    Two leakage checks, answering different questions. The per-region one asks
    "did the black box actually cover the text under it", attributed to the
    region so the studio can highlight it. ``sensitive_samples`` -- every finding
    sample whose presence in the export would be unexpected -- drives the wider
    one: "is this identifier readable ANYWHERE in the output". Without it this QA
    was structurally blind to a second occurrence of the same entity that no
    region covers, exactly as the DOCX side was before it took the same argument.

    Matching is ``contains_case_insensitive``, shared with the redactor, not a
    plain ``in``. A survivor differing only in case or a Turkish diacritic is the
    survivor this check exists to catch, and ``leakage_count`` is what stamps
    ``finding_evidence.verification_status`` as ``redacted_verified``.

    ``retained_count`` is how many findings the reviewer deliberately left
    unredacted. Those samples are excluded from the leakage set -- their text
    remaining is the expected outcome -- so without this count a PDF still
    carrying a checksum-valid national ID reports zero leakage and nothing else.
    A nonzero count keeps the report off "pass" and is stated in the checks, and
    the caller must also keep it out of the ``redacted_verified`` write. This is
    the same argument, and the same shape, as analyze_docx_export_quality().

    ``output_leaks`` is ``export_scan.scan_pdf`` over the same bytes: approved
    items found anywhere in the file -- outline, attachments, links, page
    labels, form fields, any object -- not only in its page text. Any leak
    fails the report.
    """
    text_after = extract_pdf_text_from_bytes(data)
    samples = {((text or "").strip(), standalone) for text, standalone in map(sample_match, sensitive_samples or [])}
    standalone_texts = {text for text, standalone in samples if standalone} - {
        text for text, standalone in samples if not standalone
    }
    leaked = []
    for region in regions:
        sample = (region.sample or "").strip()
        if sample and contains_case_insensitive(text_after, sample, sample in standalone_texts):
            leaked.append({"region_id": region.region_id, "finding_id": region.finding_id, "category": region.category})
    # Distinct texts rather than occurrences, so a region leak and the same
    # string found by the whole-output check are one leak, not two. Region
    # samples are folded in, so leakage_count can never read 0 while
    # leaked_regions is non-empty.
    region_texts = {(region.sample or "").strip() for region in regions}
    sensitive_texts = {(text, text in standalone_texts) for text in region_texts} | samples
    sensitive_texts = {(text, standalone) for text, standalone in sensitive_texts if text}
    leaked_sample_count = sum(
        1 for text, standalone in sensitive_texts if contains_case_insensitive(text_after, text, standalone)
    )
    # Compiling those texts leaves them in re's module-level cache as readable
    # pattern strings; the QA pass is the end of the export path.
    purge_target_pattern_cache()
    metadata_leaks = pdf_metadata_leaks(data)
    annotation_count = count_pdf_annotations(data)
    state = region_state or {}
    unreviewed = int(state.get("pending") or 0)
    checks = [
        output_scan_check(output_leaks or []),
        {
            "name": "Approved PDF regions applied",
            "status": "pass" if regions else "fail",
            "detail": f"{len(regions)} reviewed region(s) were applied.",
        },
        {
            "name": "Redacted region text not extractable (includes OCR text layers)",
            "status": "pass" if not leaked else "fail",
            "detail": f"{len(leaked)} approved region(s) still have their own text extractable underneath.",
        },
        {
            "name": "Approved sensitive text not extractable anywhere in the output",
            "status": "pass" if not leaked_sample_count else "fail",
            "detail": (
                f"{leaked_sample_count} of {len(sensitive_texts)} finding sample(s) are still readable somewhere "
                "in the redacted PDF, case- and diacritic-insensitively. This searches the whole extracted text, "
                "not only the area under each box."
            ),
        },
        {
            "name": "Identifiers deliberately retained",
            "status": "warn" if retained_count else "pass",
            "detail": (
                f"{retained_count} finding(s) were retained unredacted by reviewer decision, so their "
                "text is still in this PDF. Retained samples are excluded from the leakage count above "
                "-- a zero there does not mean the export is free of identifiers."
            ),
        },
        {
            "name": "PDF metadata scrubbed",
            "status": "pass" if not metadata_leaks else "fail",
            "detail": "The Info dictionary and XMP metadata are empty."
            if not metadata_leaks
            else "Document metadata remains and may identify a party, author or firm: "
            + ", ".join(metadata_leaks)
            + ".",
        },
        {
            "name": "No annotations or comments remain",
            "status": "pass" if annotation_count == 0 else "fail",
            "detail": "No annotations remain in the redacted output."
            if annotation_count == 0
            else f"{annotation_count} annotation(s)/comment(s) remain and may carry sensitive text.",
        },
        {
            "name": "All PDF boxes reviewed",
            "status": "pass" if unreviewed == 0 else "fail",
            "detail": "No pending PDF redaction boxes."
            if unreviewed == 0
            else f"{unreviewed} PDF box(es) are still pending review.",
        },
    ]
    if any(check["status"] == "fail" for check in checks):
        overall = "fail"
    elif any(check["status"] == "warn" for check in checks):
        overall = "warn"
    else:
        overall = "pass"
    with pymupdf.open(source_path) as source_doc:
        source_page_count = source_doc.page_count
    return {
        "format": "pdf",
        "style": "black_box",
        "overall_status": overall,
        "summary": "Reviewed PDF redaction QA completed.",
        "qa_target": qa_target,
        "region_count": len(regions),
        "leakage_count": leaked_sample_count,
        "region_leakage_count": len(leaked),
        "sensitive_sample_count": len(sensitive_texts),
        "retained_count": retained_count,
        "leaked_regions": leaked,
        "metadata_leak_count": len(metadata_leaks),
        "annotation_count": annotation_count,
        "region_state": state,
        "checks": checks,
        "source_page_count": source_page_count,
        "visual_qa": visual_qa_thumbnails(source_path, data, regions),
    }
