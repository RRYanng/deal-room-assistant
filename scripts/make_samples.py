"""Generate the SYNTHETIC SAMPLE materials used for demos and tests.

Usage:  python scripts/make_samples.py

Writes samples/<company>/... plus samples/manifest.json, which lists every file with
its company, doc_type, doc_date and whether it is uploaded in the initial demo.

All companies, people and numbers are fictional. No file contains revenue figures,
so the "missing revenue" case can be demonstrated.
"""

import csv
import json
from pathlib import Path

from fpdf import FPDF

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
BANNER = "SYNTHETIC SAMPLE - fictional company and data, created for a demo."


def write_pdf(path: Path, title: str, pages: list[list[str]]) -> None:
    """Each page is a list of paragraphs; the first paragraph of each page is its heading."""
    pdf = FPDF(format="Letter")
    pdf.set_auto_page_break(auto=True, margin=20)
    for paragraphs in pages:
        pdf.add_page()
        pdf.set_font("Helvetica", "I", 9)
        pdf.cell(0, 6, f"{BANNER}  |  {title}", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(6)
        heading, *body = paragraphs
        pdf.set_font("Helvetica", "B", 18)
        pdf.multi_cell(0, 9, heading, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(6)
        pdf.set_font("Helvetica", "", 12)
        for para in body:
            pdf.multi_cell(0, 6, para, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(6)
    pdf.output(str(path))


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "contact", "summary", "next_steps", "source"])
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "source": "SYNTHETIC SAMPLE"})


# --- Company A: Tidewell Insurance (small business insurance) ------------------

TIDEWELL_DECK = [
    ["Tidewell Insurance - Seed Pitch Deck",
     "Embedded business insurance for US small businesses.",
     "Tidewell lets small business owners buy general liability and property cover "
     "in minutes, directly inside the software they already use to run their business."],
    ["The Problem",
     "Small business owners spend weeks getting commercial insurance quotes through brokers.",
     "Many businesses end up underinsured because the process is slow and confusing."],
    ["Traction",
     "As of June 30, 2026, 100 small businesses have registered on the Tidewell platform.",
     "Registered businesses can request quotes online. Registration does not require buying a policy.",
     "Average time from registration to first quote: 4 minutes."],
    ["Funding and Ask",
     "We have raised $1.2M in pre-seed funding to date.",
     "We are raising a seed round to expand from two states to five states."],
]

TIDEWELL_NOTES = """\
# Meeting notes - Tidewell Insurance

SYNTHETIC SAMPLE - fictional company and data, created for a demo.

Date: 2026-07-22. Attendees: Dana Whitfield (Tidewell, CEO), Sam Ortiz (investment team).

## Traction

Dana said that as of June 30, 2026, Tidewell had 60 paying customers, meaning businesses with an active paid policy.

Most paying customers are restaurants and retail shops with fewer than 20 employees.

## Discussion

Sam asked about loss ratios. Dana said it is too early for reliable loss data.

The main distribution partner is a point-of-sale software provider for restaurants.

## Open questions

Revenue figures were not shared in this meeting. Follow up on pricing per policy.
"""

TIDEWELL_CRM = [
    {"date": "2026-06-05", "contact": "Dana Whitfield",
     "summary": "Intro call. Tidewell sells embedded insurance to small businesses.",
     "next_steps": "Request pitch deck."},
    {"date": "2026-07-15", "contact": "Dana Whitfield",
     "summary": "Received pitch deck by email.",
     "next_steps": "Schedule meeting with investment team."},
    {"date": "2026-07-22", "contact": "Dana Whitfield",
     "summary": "Meeting with investment team. Discussed traction and distribution partners.",
     "next_steps": "Ask for revenue figures and loss ratio data."},
]

TIDEWELL_UPDATE = """\
SYNTHETIC SAMPLE - fictional company and data, created for a demo.

Subject: Tidewell investor update - August 2026

Hi all,

Quick update on our numbers. As of June 30, 2026, we had 80 paying customers on active policies.

We also signed a second distribution partner, a booking platform for salons.

Best,
Dana Whitfield, CEO, Tidewell Insurance
"""

# --- Company B: Kitebridge Finance (small business working capital) ------------

KITEBRIDGE_DECK = [
    ["Kitebridge Finance - Series A Deck",
     "Working capital lines for small manufacturers.",
     "Kitebridge underwrites credit lines using live inventory and invoice data."],
    ["Traction",
     "As of March 31, 2026, 1,250 businesses have registered on Kitebridge.",
     "As of March 31, 2026, Kitebridge has raised $8.5M in total funding."],
]

KITEBRIDGE_NOTES = """\
# Meeting notes - Kitebridge Finance

SYNTHETIC SAMPLE - fictional company and data, created for a demo.

Date: 2026-04-20. Attendees: Lee Park (Kitebridge, CFO), Sam Ortiz (investment team).

## Traction

Lee said that as of March 31, 2026, Kitebridge had 410 paying customers with an active credit line.

## Discussion

Default rates are tracked monthly. Lee will share the default report.
"""

KITEBRIDGE_CRM = [
    {"date": "2026-04-02", "contact": "Lee Park",
     "summary": "Intro call. Kitebridge offers credit lines to small manufacturers.",
     "next_steps": "Request deck and meeting."},
    {"date": "2026-04-20", "contact": "Lee Park",
     "summary": "Meeting with investment team. Reviewed traction.",
     "next_steps": "Wait for default report."},
]


def main() -> None:
    a, b = SAMPLES / "company_a", SAMPLES / "company_b"
    a.mkdir(parents=True, exist_ok=True)
    b.mkdir(parents=True, exist_ok=True)

    write_pdf(a / "tidewell_deck.pdf", "Tidewell Insurance", TIDEWELL_DECK)
    (a / "tidewell_meeting_notes.md").write_text(TIDEWELL_NOTES, encoding="utf-8")
    write_csv(a / "tidewell_crm.csv", TIDEWELL_CRM)
    (a / "tidewell_update_2026-08.txt").write_text(TIDEWELL_UPDATE, encoding="utf-8")

    write_pdf(b / "kitebridge_deck.pdf", "Kitebridge Finance", KITEBRIDGE_DECK)
    (b / "kitebridge_meeting_notes.md").write_text(KITEBRIDGE_NOTES, encoding="utf-8")
    write_csv(b / "kitebridge_crm.csv", KITEBRIDGE_CRM)

    manifest = [
        {"company": "Tidewell Insurance", "path": "company_a/tidewell_deck.pdf",
         "doc_type": "deck", "doc_date": "2026-07-15", "initial": True},
        {"company": "Tidewell Insurance", "path": "company_a/tidewell_meeting_notes.md",
         "doc_type": "notes", "doc_date": "2026-07-22", "initial": True},
        {"company": "Tidewell Insurance", "path": "company_a/tidewell_crm.csv",
         "doc_type": "crm", "doc_date": "2026-07-22", "initial": True},
        # Conflicting paying-customer count; uploaded later in the demo.
        {"company": "Tidewell Insurance", "path": "company_a/tidewell_update_2026-08.txt",
         "doc_type": "notes", "doc_date": "2026-08-10", "initial": False},
        {"company": "Kitebridge Finance", "path": "company_b/kitebridge_deck.pdf",
         "doc_type": "deck", "doc_date": "2026-04-10", "initial": True},
        {"company": "Kitebridge Finance", "path": "company_b/kitebridge_meeting_notes.md",
         "doc_type": "notes", "doc_date": "2026-04-20", "initial": True},
        {"company": "Kitebridge Finance", "path": "company_b/kitebridge_crm.csv",
         "doc_type": "crm", "doc_date": "2026-04-20", "initial": True},
    ]
    (SAMPLES / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(manifest)} sample files to {SAMPLES}")


if __name__ == "__main__":
    main()
