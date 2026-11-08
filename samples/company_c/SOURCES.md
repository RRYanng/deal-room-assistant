# Public figures behind the ClearClaim AI sample

ClearClaim AI is a fictional company (`SYNTHETIC SAMPLE`). Its deck makes claims about the real US
dental market so the market and competitor research can check them against public sources. This
file records which public figures the planted claims are meant to be far from. Checked 2026-11-07 and 2026-11-08.

| Deck claim | Public figure | Source | Gap |
|---|---|---|---|
| "There are 400,000 independent dental practices in the United States." | "As of 2025, there are 205,088 professionally active dentists in the U.S." A practice has at least one dentist, so there are fewer practices than dentists. | ADA Health Policy Institute, The dentist workforce: https://www.ada.org/resources/research/health-policy-institute/dentist-workforce | Above a ceiling: contradicted |
| "Americans spend $400 billion a year on dental care." | "Spending for dental services increased 6.6 percent in 2024 to $189.2 billion" (CMS). The ADA Health Policy Institute, analysing the same data: "National dental expenditure was $189 billion in 2024". | CMS, National Health Expenditures 2024 Highlights: https://www.cms.gov/files/document/highlights.pdf ; ADA HPI, National dental expenditures: https://www.ada.org/resources/research/health-policy-institute/dental-care-market/national-dental-expenses | About 2.1x, above the 1.5x "possibly overstated" threshold |
| $420,000 operating costs for 14 full-time employees ($30,000 each) | Private industry, Service occupations (the lowest-cost occupation group): total compensation $23.39 per hour worked, Table 4. x 2,080 hours = $48,651 a year, the floor in code. | BLS, Employer Costs for Employee Compensation, December 2025 (released 2026-03-20), Table 4: https://www.bls.gov/news.release/archives/ecec_03202026.htm | $30,000 is below the $48,651 floor |
| "No other company offers AI agents for dental insurance claims." | Several companies sell AI insurance verification or claims automation for dental practices, for example Toothy AI and Overjet (AI Insurance Verification, June 2025). | https://www.ycombinator.com/companies/toothy-ai ; https://www.healthcaredive.com/press-release/20250603-overjet-launches-ai-powered-dental-insurance-verification | The claim is contradicted |

The deck's "15 hours per week" is a claim about the product's own results. The meeting notes quote
an industry survey (up to 20 hours a week on insurance verification): industry-level data of that
kind must not count as evidence for or against a company's own results, so the claim stays
Unverified.

Business model (added 2026-11-08): part of ClearClaim's revenue is 8% of the amounts it recovers on
previously denied claims. Fees tied to recovered amounts are common in revenue-cycle services; the
alignment check raises it as a question for the founder, not as a problem. No ClearClaim file mentions
HIPAA or a Business Associate Agreement, so the regulatory checklist shows both as "Not addressed in
materials" (absence from the materials, not a finding of non-compliance).

Changed 2026-11-08: the earlier "40% of claims denied" claim was replaced, because only vendor blogs
gave a figure for it; the YC batch of Toothy AI was removed, because it was not verified.

The earlier deck (September 2025) says 250,000 practices and a $900M market. 250,000 x $3,600 =
$900M, so that deck is internally consistent; only the change to 400,000 and $12B is planted.
