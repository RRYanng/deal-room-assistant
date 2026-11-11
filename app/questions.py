"""Questions for the founder, written by the program from verified quotes and figures only.

One question per contradicted or possibly overstated claim, per portfolio signal, per changed figure
since the last deck, and per alignment point. Each question names the figures as the sources state
them and asks for the definition or the explanation; it never states a conclusion.

Rule, enforced by only_verified_numbers(): every number in a question must appear in one of the quotes
it cites. References in parentheses (pages, files) are not checked. A question that breaks the rule is
not shown.
"""

import re
from datetime import date

NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
REFERENCE = re.compile(r"\([^()]*\)")


def numbers_in(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in NUMBER.findall(text or "")}


def only_verified_numbers(question: str, sources: list[str]) -> bool:
    allowed = set().union(*(numbers_in(s) for s in sources)) if sources else set()
    return numbers_in(REFERENCE.sub(" ", question)) <= allowed


def verified_question(text: str, sources: list[str]) -> dict | None:
    sources = [s for s in sources if s]
    return {"text": text, "sources": sources} if only_verified_numbers(text, sources) else None


def _page(item: dict) -> str:
    return (item.get("loc_label") or "").replace(item.get("filename") or "", "").strip() or item.get("filename", "")


def _position(where: str) -> tuple[int, ...]:
    """(page, paragraph) from a location such as "p.5 ¶2"."""
    return tuple(int(n) for n in re.findall(r"\d+", where))


# --- claims -----------------------------------------------------------------------------------

def claim_question(claim: dict, claims: list[dict]) -> dict | None:
    if claim["status"] not in ("contradicted", "possibly_overstated") or not claim["evidence"]:
        return None
    first = {c["key"]: c for c in reversed(claims)}  # the first claim of each key
    deck, where = claim.get("value_raw"), _page(claim)
    e = claim["evidence"][0]  # evidence is ordered from most to least neutral

    if e["source"] == "calculation" and claim["key"] == "market.size_usd":
        count, price = first.get("market.customer_count"), first.get("market.price_usd")
        if count and price:
            return verified_question(f"The deck states {count['value_raw']} target customers at {price['value_raw']} each and a "
                            f"market size of {deck} ({where}). How was {deck} calculated?",
                            [count["quote"], price["quote"], claim["quote"]])
    if e["source"] == "calculation" and claim["key"] in ("finance.opex_annual_usd", "team.headcount"):
        opex, heads = first.get("finance.opex_annual_usd"), first.get("team.headcount")
        if opex and heads:
            return verified_question(f"The deck states {heads['value_raw']} full-time employees and {opex['value_raw']} in annual "
                            f"operating costs. What do the operating costs include, and are all {heads['value_raw']} "
                            f"employees paid full-time?", [heads["quote"], opex["quote"]])
    if e["source"] == "internal" and e["stance"] == "overstates":
        return verified_question(f"The deck names this risk: \"{e['quote']}\" ({_page(e)}). The projections assume \"{claim['quote']}\" "
                        f"How do the projections change if that risk happens?", [e["quote"], claim["quote"]])
    if e["source"] == "internal" and e.get("value_raw"):
        if e.get("filename") == claim.get("filename"):
            # In page order, so both claims of the pair ask the same question.
            (a, at), (b, bt) = sorted([(deck, where), (e["value_raw"], _page(e))], key=lambda v: _position(v[1]))
            return verified_question(f"{claim['label']} is stated as {a} ({at}) and {b} ({bt}). "
                            f"Which figure is current, and how is each defined?", [claim["quote"], e["quote"]])
        doc = "the CRM" if (e.get("filename") or "").endswith(".csv") else "the meeting notes"
        return verified_question(f"The deck says {deck} ({where}), but {doc} say {e['value_raw']} ({e.get('filename')}). "
                        f"Which is right, and as of what date?", [claim["quote"], e["quote"]])
    if e["source"] == "external" and e.get("value_raw"):
        source = e["headline"].split(":")[0]
        if "upper bound" in e.get("note", ""):
            return verified_question(f"{source} reports {e['value_raw']}, which sets an upper limit for this figure, but the deck "
                            f"says {deck} ({where}). How was {deck} counted, and from which source?",
                            [claim["quote"], e["quote"]])
        return verified_question(f"{source} reports {e['value_raw']}; the deck says {deck} ({where}). Which source and "
                        f"definition does the deck use?", [claim["quote"], e["quote"]])
    if e["source"] == "external":
        names = re.search(r"in this market: (.*)\.$", e.get("note", ""))
        if names:
            listed = ", ".join(n.strip() for n in names.group(1).split(",")[:3])
            return verified_question(f"The deck says \"{claim['quote']}\" ({where}), and web research found companies offering "
                            f"similar products, such as {listed}. How does the company differ from them?",
                            [claim["quote"], e.get("quote") or ""])
    if e["source"] == "internal" and e.get("quote"):
        return verified_question(f"The deck says \"{claim['quote']}\" ({where}), but \"{e['quote']}\" ({e.get('filename')}). "
                        f"Which is accurate?", [claim["quote"], e["quote"]])
    return None


# --- portfolio signals ---------------------------------------------------------------------------

def _as_written(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def signal_question(signal: dict) -> dict | None:
    quotes = [e["quote"] for e in signal["evidence"]]
    last = signal["evidence"][-1] if signal["evidence"] else {}
    if signal["kind"] == "cash_runway":
        cash, burn = re.match(r"(\S+) cash / (\S+) monthly net burn", signal["detail"]).groups()
        return verified_question(f"The update reports {cash} in cash and {burn} in monthly net burn "
                        f"({last.get('filename')}). How long will the cash last, and what is the plan to extend it?",
                        quotes)
    if signal["kind"] == "key_person_departure":
        said = next((e for e in signal["evidence"] if e.get("status") == "departed"), None)
        if said:
            return verified_question(f"The update says: \"{said['quote']}\" ({said['filename']}). Who covers this role now, and "
                            f"what is the hiring plan?", quotes)
        return verified_question(f"{signal['title'].removesuffix(' departed')} is no longer in the team list. Has this person "
                        f"left, and who covers the role?", quotes)
    if signal["kind"] == "milestone_slip":
        targets = [e["target_date"] for e in signal["evidence"] if e.get("target_date")]
        name = signal["title"].removeprefix("Milestone slipping: ")
        if len(targets) >= 2:
            return verified_question(f"The target for {name} moved from {_as_written(targets[0])} to {_as_written(targets[-1])} "
                            f"across the updates. What is blocking it, and how firm is the new date?", quotes)
        return verified_question(f"The target date for {name} has passed. What is its status, and what is the new date?", quotes)
    return None


# --- changes since the last deck -------------------------------------------------------------------

def change_question(change: dict) -> dict | None:
    if change["kind"] != "changed":
        return None
    old, new = change["old"], change["new"]
    return verified_question(f"{change['label']} was {old['value_raw']} in the earlier deck ({old['filename']}) and is "
                    f"{new['value_raw']} in this deck ({new['filename']}). What changed: the definition, the source, "
                    f"or the business?", [old["quote"], new["quote"]])
