"""Parsing and chunk locations, on the real sample files and small hand-made inputs."""

from pathlib import Path

import pytest
from fpdf import FPDF

from app.ingest import IngestError, parse_file, validate_upload

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def chunks_of(rel_path):
    path = SAMPLES / rel_path
    return parse_file(path.name, path.read_bytes()).chunks


def find(chunks, needle):
    matches = [c for c in chunks if needle in c.text]
    assert len(matches) == 1, f"{needle!r} found in {len(matches)} chunks"
    return matches[0]


def test_pdf_chunks_have_page_and_paragraph():
    c = find(chunks_of("company_a/tidewell_deck.pdf"), "100 small businesses have registered")
    assert (c.page, c.para, c.row_num) == (3, 2, None)
    assert c.loc_label == "tidewell_deck.pdf p.3 ¶2"
    assert c.text.startswith("Traction\n")  # heading attached to its paragraph


def test_pdf_wrapped_lines_are_joined_into_one_paragraph():
    c = find(chunks_of("company_a/tidewell_deck.pdf"), "general liability")
    assert "directly inside the software" in c.text
    assert "\n" not in c.text


def test_markdown_paragraphs_and_headings():
    c = find(chunks_of("company_a/tidewell_meeting_notes.md"), "60 paying customers")
    assert (c.page, c.para) == (None, 3)
    assert c.loc_label == "tidewell_meeting_notes.md ¶3"
    assert c.text.startswith("Traction\n")
    assert "#" not in c.text


def test_txt_paragraphs():
    c = find(chunks_of("company_a/tidewell_update_2026-08.txt"), "80 paying customers")
    assert c.loc_label == "tidewell_update_2026-08.txt ¶2"


def test_csv_one_chunk_per_row_with_row_number():
    doc = parse_file("crm.csv", (SAMPLES / "company_a/tidewell_crm.csv").read_bytes())
    assert [c.loc_label for c in doc.chunks] == ["crm.csv row 1", "crm.csv row 2", "crm.csv row 3"]
    assert doc.chunks[2].row_num == 3
    assert "next_steps: Ask for revenue figures" in doc.chunks[2].text
    assert doc.crm_rows[0] == {"date": "2026-06-05", "contact": "Dana Whitfield",
                               "summary": "Intro call. Tidewell sells embedded insurance to small businesses.",
                               "next_steps": "Request pitch deck.", "row_num": 1}


def test_csv_header_is_case_insensitive_and_blank_rows_skipped():
    data = b"Date,Contact,Summary,Next_Steps\n2026-01-02,Ann,Hello,Call back\n,,,\n"
    doc = parse_file("crm.csv", data)
    assert len(doc.chunks) == 1 and doc.crm_rows[0]["contact"] == "Ann"


def test_long_paragraph_split_into_parts_with_same_paragraph_number():
    para = " ".join(f"Sentence number {i} is here." for i in range(120))  # ~3,300 chars
    doc = parse_file("long.txt", f"Intro paragraph.\n\n{para}".encode())
    parts = [c for c in doc.chunks if c.para == 2]
    assert len(parts) == 3
    assert all(len(c.text) <= 1200 for c in parts)
    assert parts[0].loc_label == "long.txt ¶2 part 1/3"
    assert " ".join(c.text for c in parts) == para


# --- errors ---------------------------------------------------------------------

def test_crm_missing_columns_has_clear_message():
    with pytest.raises(IngestError) as e:
        parse_file("crm.csv", b"date,summary\n2026-01-01,hi\n")
    assert "missing required CRM column(s): contact, next_steps" in str(e.value)
    assert "Expected columns: date, contact, summary, next_steps" in str(e.value)


def test_crm_bad_date_has_row_number():
    with pytest.raises(IngestError, match="crm.csv row 2: date '03/01/2026' must be in YYYY-MM-DD"):
        parse_file("crm.csv", b"date,contact,summary,next_steps\n2026-01-01,a,b,c\n03/01/2026,a,b,c\n")


@pytest.mark.parametrize("filename,doc_type", [("deck.docx", "deck"), ("notes.xlsx", "notes"),
                                               ("crm.xlsx", "crm"), ("noext", "notes")])
def test_unsupported_formats_rejected(filename, doc_type):
    with pytest.raises(IngestError) as e:
        validate_upload(filename, doc_type, None)
    assert ".csv" in str(e.value)  # message lists what is supported


def test_unknown_doc_type_and_bad_doc_date_rejected():
    with pytest.raises(IngestError, match="Unknown material type 'memo'"):
        validate_upload("a.md", "memo", None)
    with pytest.raises(IngestError, match="YYYY-MM-DD"):
        validate_upload("a.md", "notes", "July 2026")


def test_empty_and_non_utf8_files_rejected():
    with pytest.raises(IngestError, match="is empty"):
        parse_file("a.md", b"")
    with pytest.raises(IngestError, match="not valid UTF-8"):
        parse_file("a.txt", "café".encode("latin-1"))


def test_pdf_without_text_rejected():
    pdf = FPDF()
    pdf.add_page()  # blank page: no text layer, like a scan
    with pytest.raises(IngestError, match="no extractable text"):
        parse_file("scan.pdf", bytes(pdf.output()))


def test_corrupt_pdf_rejected():
    with pytest.raises(IngestError, match="Could not read broken.pdf as a PDF"):
        parse_file("broken.pdf", b"%PDF-1.4 this is not really a pdf")
