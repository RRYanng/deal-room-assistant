"""Run the v2 evaluation cases offline and print a Markdown table of results.

Usage:  python scripts/eval_report.py

An evaluation case is every test named test_e<N>_... (several tests can belong to one case); a case
passes only if all of its tests pass. Also runs the whole offline suite, so the table shows the v1
regression count. Uses FakeLLM and FakeWebSearch only: no model or web calls.
"""

import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CASES = {
    "E1": "Gross margin stated two ways in one deck (67-82% vs above 90%) is Contradicted",
    "E2": "Market size arithmetic that does not add up ($12B vs 400,000 x $3,600) is Contradicted",
    "E3": "Operating cost per employee below the BLS-based floor is Possibly overstated",
    "E4": "A projection that assumes away a risk the deck states is Possibly overstated",
    "E5": "A claim with no evidence (15 hours saved per week) stays Unverified",
    "E6": "A claim backed by notes and CRM is Consistent with company data",
    "E7": "400,000 practices vs public data (205,088 dentists) is Possibly overstated, with a real link",
    "E8": "40% denial rate vs public data (19.3%) is Possibly overstated, with a real link",
    "E9": "Competitor research lists competitors with links and contradicts 'no other company'",
    "E10": "Invented sources and altered quotes are dropped; queries naming the company are refused",
    "E13": "Flags are not findings until confirmed; dismissed stays quiet unless evidence changes; reason required",
    "FP": "The same file checked twice gives identical fingerprints",
}
FINGERPRINT_TEST = "test_same_file_checked_twice_gives_identical_fingerprints"


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        xml_path = Path(tmp) / "results.xml"
        subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml_path}"],
                       cwd=ROOT, capture_output=True, text=True)
        cases = list(ET.parse(xml_path).getroot().iter("testcase"))

    def passed(tc) -> bool:
        return not any(child.tag in ("failure", "error", "skipped") for child in tc)

    by_case: dict[str, list[bool]] = {}
    for tc in cases:
        name = tc.get("name", "")
        found = re.match(r"test_(e\d+)_", name)
        key = found.group(1).upper() if found else ("FP" if name == FINGERPRINT_TEST else None)
        if key:
            by_case.setdefault(key, []).append(passed(tc))

    total, ok = len(cases), sum(passed(tc) for tc in cases)
    print("| Case | What is checked | Tests | Result |")
    print("|---|---|---|---|")
    for key, text in CASES.items():
        results = by_case.get(key, [])
        result = "not run" if not results else ("pass" if all(results) else "FAIL")
        print(f"| {key} | {text} | {len(results)} | {result} |")
    print(f"\nWhole offline suite: {ok} of {total} tests passed.")
    return 0 if ok == total and all(all(by_case.get(k, [False])) for k in CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
