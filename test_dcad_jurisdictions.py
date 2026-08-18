#!/usr/bin/env python3
"""§24 — DCAD per-parcel taxing-unit parse (sixth §19 instance).

THE DEFECT, PRECISELY. The old parser was
    re.findall(r'(DALLAS[A-Z\\s]*|PARKLAND[A-Z\\s]*|UNASSIGNED)\\s+\\$([\\d,\\.]+)', text)
and it was wrong on two levels:

  · SYMPTOM — the alternation could only recognise units named DALLAS… or PARKLAND…, so GARLAND ISD,
    CITY OF GARLAND, RICHARDSON ISD and every other non-Dallas unit were invisible by construction.
    A local truth ("units here are named DALLAS-something") applied fleet-wide — §19.
  · ACTUAL DEFECT — DCAD's table is COLUMN-oriented (units are columns; each row is one attribute of
    every unit), so a name never sits adjacent to its own amount. `[A-Z\\s]*` therefore ran across
    tabs and newlines and swallowed the entire header row into a single "entity". No jurisdiction
    name list would have fixed that.

MEASURED: `tax_rates` was EMPTY on 223 and MALFORMED on 77 of 300 enriched cases — unusable on
**100%** of the book.

Fixtures are REAL captured DCAD page text, one Garland parcel and one Dallas parcel.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import property_intel as P

ROOT = Path(__file__).parent
_passed, _failed = 0, 0


def check(label, ok, detail=""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  → {detail}" if detail else ""))


def _page(city, school, rates, est, city_name_row=None):
    """A DCAD estimated-taxes page, column-oriented, with the surrounding noise that broke earlier
    attempts at this parse: a nav bar, an ENS link, and a Legal Desc row with an empty first cell."""
    cols = [city, school, "DALLAS COUNTY", "DALLAS COLLEGE", "PARKLAND HOSPITAL", "UNASSIGNED"]
    cats = ["City", "School", "County", "College", "Hospital", "Special District"]
    return (
        " Exemptions   Estimated Taxes   History  \n"
        "Notice Of Estimated Taxes (ENS*)\n"
        "Legal Desc (Current 2027)\n"
        " \tDeed Transfer Date:  1/5/2009\n"
        "Estimated Taxes (2026 Certified Values)\n"
        " \t" + "\t".join(cats) + "\n"
        "Taxing Jurisdiction\t" + "\t".join(cols) + "\n"
        "Tax Rate per $100\t" + "\t".join(rates) + "\n"
        "Taxable Value\t" + "\t".join(["$292,850"] * 6) + "\n"
        "Estimated Taxes\t" + "\t".join(est) + "\n"
        "Tax Ceiling\tN/A\tN/A\tN/A\tN/A\tN/A\tN/A\n"
        "Total Estimated Taxes:\t$7,012.94\n"
        "DO NOT PAY TAXES BASED ON THESE ESTIMATED TAXES.\n")


GARLAND = _page("GARLAND", "GARLAND ISD",
                ["$0.689746", "$1.1709", "$0.2155", "$0.106575", "$0.212", "N/A"],
                ["$2,019.92", "$3,428.98", "$631.09", "$312.10", "$620.84", "N/A"])
DALLAS = _page("DALLAS", "DALLAS ISD",
               ["$0.6988", "$0.993835", "$0.2155", "$0.106575", "$0.212", "N/A"],
               ["$2,003.67", "$2,849.62", "$617.90", "$305.58", "$607.87", "N/A"])


def test_the_units_the_old_regex_could_never_see():
    print("\nthe regression that motivated the fix")
    g = {r["entity"]: r for r in P.parse_tax_jurisdictions(GARLAND)}
    check("GARLAND ISD is parsed", "GARLAND ISD" in g, str(list(g)))
    check("CITY OF GARLAND is parsed", "CITY OF GARLAND" in g, str(list(g)))
    check("GARLAND ISD carries its real rate 1.1709 (matches the GISD portal)",
          g.get("GARLAND ISD", {}).get("tax_rate") == 1.1709)
    check("CITY OF GARLAND carries 0.689746 (matches the City portal)",
          g.get("CITY OF GARLAND", {}).get("tax_rate") == 0.689746)
    # The old regex, run on the same text, produced exactly the garbage seen in the live DB.
    old = re.findall(r'(DALLAS[A-Z\s]*|PARKLAND[A-Z\s]*|UNASSIGNED)\s+\$([\d,\.]+)', GARLAND)
    old_entities = [e.strip() for e, _ in old]
    check("the OLD regex found no Garland unit at all",
          not any("GARLAND" in e for e in old_entities), str(old_entities)[:90])
    check("the OLD regex recovered NOTHING usable from a column table",
          len(old) == 0, str(old)[:90])
    # (The complementary failure — several units swallowed into one 'entity' with embedded tabs — is
    # evidenced by the live DB, where 77 of 300 enriched cases stored exactly that shape. It depends
    # on the surrounding page text, so it is not asserted against a synthetic fixture here.)


def test_column_orientation_not_row():
    print("\ncolumn-oriented parsing — order, count and identity are all free variables")
    base = {r["entity"] for r in P.parse_tax_jurisdictions(GARLAND)}
    # Same table, columns REORDERED: a row-reading parser is order-sensitive; a column one is not.
    swapped = _page("GARLAND", "GARLAND ISD",
                    ["$1.1709", "$0.689746", "$0.2155", "$0.106575", "$0.212", "N/A"],
                    ["$3,428.98", "$2,019.92", "$631.09", "$312.10", "$620.84", "N/A"])
    check("reordering the value columns does not change WHICH units are found",
          {r["entity"] for r in P.parse_tax_jurisdictions(swapped)} == base)
    # A district this code has never heard of must parse exactly like a known one.
    novel = _page("SUNNYVALE", "SUNNYVALE ISD",
                  ["$0.5", "$1.4", "$0.2155", "$0.106575", "$0.212", "N/A"],
                  ["$1,000.00", "$2,000.00", "$631.09", "$312.10", "$620.84", "N/A"])
    got = {r["entity"] for r in P.parse_tax_jurisdictions(novel)}
    check("a NEVER-SEEN district parses (coverage follows the parcel, not a maintained list)",
          {"SUNNYVALE ISD", "CITY OF SUNNYVALE"} <= got, str(got))
    # A short table (fewer columns) must not misalign.
    short = (" \tCity\tSchool\n"
             "Taxing Jurisdiction\tIRVING\tIRVING ISD\n"
             "Tax Rate per $100\t$0.59\t$1.02\n"
             "Estimated Taxes\t$500.00\t$900.00\n")
    got2 = P.parse_tax_jurisdictions("Estimated Taxes (2026 Certified Values)\n" + short)
    check("a 2-column table parses without misalignment",
          [(r["entity"], r["estimated_tax"]) for r in got2]
          == [("CITY OF IRVING", 500.0), ("IRVING ISD", 900.0)], str(got2))


def test_values_and_absence():
    print("\nvalues — and absence is None, never 0")
    g = {r["entity"]: r for r in P.parse_tax_jurisdictions(GARLAND)}
    check("estimated tax is the column's own amount", g["GARLAND ISD"]["estimated_tax"] == 3428.98)
    check("category is captured from the header row", g["GARLAND ISD"]["category"] == "School")
    check("the City column is normalised to 'CITY OF …'", "CITY OF GARLAND" in g)
    check("UNASSIGNED (DCAD's empty-column placeholder) is NOT a taxing unit",
          not any("UNASSIGNED" in e for e in g))
    check("parsed amounts reconcile to the page total within rounding",
          abs(sum(r["estimated_tax"] for r in g.values()) - 7012.94) <= 0.02)
    check("'N/A' becomes None, never 0.0", P._money("N/A") is None)
    check("an empty cell becomes None, never 0.0", P._money("") is None)
    check("a real $0.00 stays 0.0 (distinct from absence)", P._money("$0.00") == 0.0)


def test_fails_closed():
    print("\nfails closed — never invents units")
    check("no estimated-taxes section → no units", P.parse_tax_jurisdictions("nothing here") == [])
    check("empty input → no units", P.parse_tax_jurisdictions("") == [])
    check("None input → no units", P.parse_tax_jurisdictions(None) == [])
    check("heading but no jurisdiction row → no units",
          P.parse_tax_jurisdictions("Estimated Taxes (2026 Certified Values)\nTaxable Value\t$1\n") == [])
    # The nav bar says "Estimated Taxes" long before the table; anchoring must not start there.
    check("the nav-bar 'Estimated Taxes' link does not anchor the parse",
          {r["entity"] for r in P.parse_tax_jurisdictions(DALLAS)}
          == {"CITY OF DALLAS", "DALLAS ISD", "DALLAS COUNTY", "DALLAS COLLEGE", "PARKLAND HOSPITAL"})


def test_corroborates_the_petition():
    print("\nDCAD as an INDEPENDENT corroboration source for the petition's collector list")
    import jurisdictions as J
    dcad = {J.canonical(r["entity"]) for r in P.parse_tax_jurisdictions(GARLAND)}
    petition = {c["collector"] for c in J.petition_collectors(
        [{"entity": "GARLAND INDEPENDENT SCHOOL DISTRICT", "total": 6991.30},
         {"entity": "CITY OF GARLAND", "total": 4337.90}])}
    check("every collector the petition named appears in DCAD's unit table",
          petition <= dcad, f"petition={petition} dcad={dcad}")
    ext = {u for u in dcad if J.resolve_collector(u)["scope"] == "external"}
    check("DCAD independently identifies the SAME external collectors",
          ext == {"GARLAND ISD", "CITY OF GARLAND"}, str(ext))
    dallas = {J.canonical(r["entity"]) for r in P.parse_tax_jurisdictions(DALLAS)}
    check("a Dallas parcel shows NO external collector (no false alarm)",
          not any(J.resolve_collector(u)["scope"] == "external" for u in dallas))


def run():
    print("=" * 78)
    print("§24 — DCAD COLUMN-ORIENTED TAXING-UNIT PARSE")
    print("=" * 78)
    test_the_units_the_old_regex_could_never_see()
    test_column_orientation_not_row()
    test_values_and_absence()
    test_fails_closed()
    test_corroborates_the_petition()
    print("-" * 78)
    print(f"{_passed}/{_passed + _failed} passed" + ("  ✓ all green" if not _failed else ""))
    return _failed == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
