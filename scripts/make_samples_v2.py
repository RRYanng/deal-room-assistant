"""Generate the v2 SYNTHETIC SAMPLE company, ClearClaim AI, used for claim checks and monitoring.

Usage:  python scripts/make_samples_v2.py

Writes samples/company_c/... plus samples/manifest_v2.json. The v1 files and samples/manifest.json
are not touched, so the v1 demo and its tests stay as they are.

ClearClaim AI, its people, its payer and every number are fictional. The deck plants the problems
the claim check must find; samples/company_c/SOURCES.md lists the public figures the market claims
are meant to be checked against.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from make_samples import SAMPLES, write_csv, write_pdf  # noqa: E402

COMPANY = "ClearClaim AI"

# --- Earlier deck (September 2025): consistent numbers, used for "changed since last check" ---

DECK_2025_09 = [
    ["ClearClaim AI - Pre-Seed Deck",
     "AI agents that verify dental insurance and file claims for independent dental practices."],
    ["The Problem",
     "Front-desk staff at dental practices spend hours every day on the phone with insurers.",
     "Americans spend $400 billion a year on dental care."],
    ["Market",
     "There are 250,000 independent dental practices in the United States.",
     "At $3,600 per practice per year, our total addressable market is $900M."],
    ["Traction",
     "As of August 31, 2025, 40 dental practices pay for ClearClaim."],
    ["Team",
     "We are a team of 9 full-time employees."],
]

# --- Main deck (December 2025): every planted problem lives here ---------------------------

DECK_2025_12 = [
    ["ClearClaim AI - Seed Pitch Deck",
     "AI agents that verify dental insurance and file claims for independent dental practices.",
     "ClearClaim connects to a practice's management software, checks each patient's coverage "
     "before the visit, and submits clean claims to payers."],
    ["The Problem",
     "Front-desk staff at dental practices spend hours every day on the phone with insurers.",
     # Overstated: CMS puts US dental services spending at $189.2 billion in 2024 (SOURCES.md).
     "Americans spend $400 billion a year on dental care.",
     "Dental practices cannot hire enough front-desk staff to keep up."],
    ["Market",
     # Overstated: there are about 205,000 dentists in total, so practices are fewer still.
     "There are 400,000 independent dental practices in the United States.",
     # Arithmetic fails: 400,000 x $3,600 = $1.44B, not $12B.
     "At $3,600 per practice per year, our total addressable market is $12B."],
    ["Traction",
     # Consistent with the meeting notes and the CRM.
     "As of November 30, 2025, 85 dental practices pay for ClearClaim.",
     # No evidence anywhere: must stay unverified.
     "ClearClaim saves front-desk staff 15 hours per week per practice."],
    ["Product and Unit Economics",
     # Contradicted by "above 90%" on the financial plan page.
     "Gross margin is 67-82%, depending on payer mix and the share of claims that need manual review.",
     "Every claim is checked against the payer's rules before it is submitted."],
    ["Competition",
     # Contradicted by competitor research.
     "No other company offers AI agents for dental insurance claims.",
     "Today practices rely on manual work or outsourced billing services."],
    ["Team",
     # With the operating costs below: $30,000 per employee, below the $48,651 floor.
     "We are a team of 14 full-time employees.",
     "Maya Chen, CEO, is a former dental office manager. Daniel Okafor, CTO, is a former payments "
     "engineer. Priya Raman, Head of Sales, sold practice software for eight years."],
    ["Risks",
     # Stated risk that the projections assume away.
     "Payers may change claim submission rules or restrict clearinghouse access, which could lower "
     "first-pass acceptance and increase customer churn."],
    ["Financial Plan",
     "Total annual operating costs for 2025 are $420,000.",
     "Current gross margin: above 90%.",
     "Our model assumes 0% customer churn and 98% first-pass claim acceptance through 2029."],
    ["Business Model",
     # Alignment discussion point: part of the revenue is a share of what is recovered. Common in the
     # industry; a question to ask, not a problem in itself.
     "Practices pay a monthly subscription per location, plus 8% of the amounts ClearClaim recovers on "
     "previously denied claims.",
     "Practices get paid faster and spend less staff time on insurance; patients get clearer cost estimates "
     "before treatment."],
    # Not mentioned anywhere: HIPAA or a Business Associate Agreement (regulatory checklist).
]

NOTES_2025_12 = """\
# Meeting notes - ClearClaim AI

SYNTHETIC SAMPLE - fictional company and data, created for a demo.

Date: 2025-12-08. Attendees: Maya Chen (ClearClaim, CEO), Sam Ortiz (investment team).

## Traction

Maya said that as of November 30, 2025, 85 dental practices were paying for ClearClaim.

Most customers are single-location practices with one to three dentists.

## Margins

Maya said gross margin is around 70% today, because about a quarter of claims still need manual review by the operations team.

## Product

Maya said practices tell them the product saves a lot of front-desk time. ClearClaim has not run a time study yet.

Maya cited an industry survey that says front desks spend up to 20 hours a week on insurance verification.

## Open questions

Ask for the payer mix and churn to date.
"""

CRM_2025_12 = [
    {"date": "2025-10-02", "contact": "Maya Chen",
     "summary": "Intro call. ClearClaim automates insurance verification and claims for dental practices.",
     "next_steps": "Request the deck."},
    {"date": "2025-12-01", "contact": "Maya Chen",
     "summary": "Received the updated seed deck by email.",
     "next_steps": "Schedule a meeting with the investment team."},
    {"date": "2025-12-08", "contact": "Maya Chen",
     "summary": "Meeting with investment team. Reviewed traction: 85 paying practices as of November 30, 2025.",
     "next_steps": "Ask for payer mix and churn data."},
]

# --- Quarterly updates after the investment ----------------------------------------------
# Runway: Q1 4.4M / 200k = 22 months; Q2 3.3M / 300k = 11 (warning); Q3 2.1M / 300k = 7 with
# revenue up only 2% (high). The CTO leaves in Q3. The payer integration slips in Q2 and again in Q3.

UPDATE_Q1 = """\
SYNTHETIC SAMPLE - fictional company and data, created for a demo.

Subject: ClearClaim AI investor update - Q1 2026

Hi all,

As of March 31, 2026, we had $4.4M in cash. Net burn averaged $200,000 per month in Q1.

Revenue for Q1 2026 was $310,000, up 15% from Q4 2025. We ended the quarter with 120 paying practices.

Team: Maya Chen (CEO), Daniel Okafor (CTO) and Priya Raman (Head of Sales) lead the company.

Milestone: direct integration with Bluecrest Dental Plan, target date May 31, 2026.

Best,
Maya Chen, CEO, ClearClaim AI
"""

UPDATE_Q2 = """\
SYNTHETIC SAMPLE - fictional company and data, created for a demo.

Subject: ClearClaim AI investor update - Q2 2026

Hi all,

As of June 30, 2026, we had $3.3M in cash. Net burn averaged $300,000 per month in Q2, as we added two engineers.

Revenue for Q2 2026 was $357,000, up 15% from Q1 2026. We ended the quarter with 138 paying practices.

Team: Maya Chen (CEO), Daniel Okafor (CTO) and Priya Raman (Head of Sales) lead the company.

Milestone: the Bluecrest Dental Plan integration is now expected on August 31, 2026 (previously May 31, 2026).

Best,
Maya Chen, CEO, ClearClaim AI
"""

UPDATE_Q3 = """\
SYNTHETIC SAMPLE - fictional company and data, created for a demo.

Subject: ClearClaim AI investor update - Q3 2026

Hi all,

As of September 30, 2026, we had $2.1M in cash. Net burn averaged $300,000 per month in Q3.

Revenue for Q3 2026 was $364,000, up 2% from Q2 2026. We ended the quarter with 141 paying practices.

Daniel Okafor, our CTO, left the company in August. Maya is covering engineering while we hire.

Team: Maya Chen (CEO) and Priya Raman (Head of Sales) lead the company.

Milestone: the Bluecrest Dental Plan integration is now expected on November 30, 2026 (previously August 31, 2026).

Best,
Maya Chen, CEO, ClearClaim AI
"""


def main() -> None:
    c = SAMPLES / "company_c"
    c.mkdir(parents=True, exist_ok=True)

    write_pdf(c / "clearclaim_deck_2025-09.pdf", COMPANY, DECK_2025_09)
    write_pdf(c / "clearclaim_deck_2025-12.pdf", COMPANY, DECK_2025_12)
    (c / "clearclaim_meeting_notes.md").write_text(NOTES_2025_12, encoding="utf-8")
    write_csv(c / "clearclaim_crm.csv", CRM_2025_12)
    (c / "clearclaim_update_2026-Q1.txt").write_text(UPDATE_Q1, encoding="utf-8")
    (c / "clearclaim_update_2026-Q2.txt").write_text(UPDATE_Q2, encoding="utf-8")
    (c / "clearclaim_update_2026-Q3.txt").write_text(UPDATE_Q3, encoding="utf-8")

    def entry(path, doc_type, doc_date, stage):
        return {"company": COMPANY, "path": f"company_c/{path}", "doc_type": doc_type,
                "doc_date": doc_date, "stage": stage}

    manifest = [
        entry("clearclaim_deck_2025-09.pdf", "deck", "2025-09-15", "diligence"),
        entry("clearclaim_deck_2025-12.pdf", "deck", "2025-12-01", "diligence"),
        entry("clearclaim_meeting_notes.md", "notes", "2025-12-08", "diligence"),
        entry("clearclaim_crm.csv", "crm", "2025-12-08", "diligence"),
        entry("clearclaim_update_2026-Q1.txt", "notes", "2026-04-10", "portfolio"),
        entry("clearclaim_update_2026-Q2.txt", "notes", "2026-07-10", "portfolio"),
        entry("clearclaim_update_2026-Q3.txt", "notes", "2026-10-10", "portfolio"),
    ]
    (SAMPLES / "manifest_v2.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(manifest)} sample files to {c}")


if __name__ == "__main__":
    main()
