-- Which generated PDF box is the place a finding was found, and which is a
-- repeat of its text found by searching the page.
--
-- Region generation boxes every match of a finding's sample on every page (the
-- backstop: a repeat detection missed is boxed too), and each box is approved or
-- rejected on its own. 'finding' marks the box at the finding's own occurrence,
-- 'repeat' every other box of that finding, so the reviewer can tell them
-- apart. A box with no row here could not be told apart (a drawn box, an OCR
-- token box, or a page whose matches did not line up with the extracted text),
-- and is shown without either label.
--
-- A table beside pdf_redaction_regions, not a column of it: an ALTER TABLE ADD
-- COLUMN is not idempotent, and a database built from db/schema.sql (which must
-- already carry the end state) would then fail this migration with a
-- duplicate column.

CREATE TABLE IF NOT EXISTS pdf_region_occurrences (
    region_id INTEGER PRIMARY KEY,
    occurrence TEXT NOT NULL CHECK (occurrence IN ('finding', 'repeat')),
    FOREIGN KEY (region_id) REFERENCES pdf_redaction_regions(id) ON DELETE CASCADE
);
