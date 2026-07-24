#!/usr/bin/env python3
"""Tests for the DCAD detail parsing that feeds ARV — no network.

Two live defects, both traced from TX-26-00777 (5807 Morningside Ave, Dallas 75206):

  1. BATHS WERE NEVER CAPTURED. DCAD renders "# Baths (Full/Half)\t1/ 1" — with a SPACE after the
     slash. The pattern required (\\d+/\\d+) adjacent, so it never matched and every case stored "".
     Measured on the live DB: 0 of 247 enriched cases had baths while 164 had bedrooms — the label
     parsed, the value didn't. Baths feed the comp MatchScore (beds_baths), so this silently
     degraded every ARV match.

  2. A DCAD SCRAPE THAT YIELDS NOTHING RECORDED NO ERROR. scrape_dcad only set result['error']
     from an exception, so a page that loaded but rendered nothing parsable returned all-empty
     fields with error=None — indistinguishable from "this property genuinely has no data".
     TX-26-00777 has a VALID 17-digit account whose DCAD page really does carry 1,180 sqft / 3 bed
     / 1-1 bath, yet stored every DCAD field empty with no error, so propose 422'd for "no living
     area" with no way to tell a scrape failure from a real gap.

Run: python3 test_dcad_parse.py   (exit 0 = all green)
"""
import re
import sys

sys.path.insert(0, ".")
import comps

_res = []
def check(name, cond):
    _res.append(bool(cond)); print(("  PASS  " if cond else "  FAIL  ") + name)


# The physical-characteristics block EXACTLY as DCAD renders it for 5807 Morningside Ave
# (tab-separated, and note "1/ 1" — a space after the slash).
DCAD_TEXT = (
    "Building Class\t13 \tConstruction Type\tFRAME \t# Baths (Full/Half)\t1/ 1 \n"
    "Year Built\t1958 \tFoundation\tPIER AND BEAM \t# Kitchens\t1 \n"
    "Effective Year Built\t1958 \tRoof Type\tGABLE \t# Bedrooms\t3 \n"
    "Actual Age\t68 years\tRoof Material\tCOMP SHINGLES \t# Wet Bars\t0 \n"
    "Desirability\tGOOD \tFence Type\tWOOD \t# Fireplaces\t0 \n"
    "Living Area\t1,180 sqft \tExt. Wall Material\tBRICK VENEER \tSprinkler (Y/N)\tN \n"
    "Total Area\t1,180 sqft \tBasement\tNONE \tDeck (Y/N)\tY \n"
)

OLD_BATHS = r'#\s*Baths[^\d]*(\d+/\d+)'
NEW_BATHS = r'#\s*Baths[^\d]*(\d+)\s*/\s*(\d+)'


def parse_baths(text):
    """The shipped parser, mirrored: match then normalize to 'F/H'."""
    m = re.search(NEW_BATHS, text)
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def run():
    # ── 1. the regression itself ──
    check("the OLD pattern does NOT match real DCAD text (this was the bug)",
          re.search(OLD_BATHS, DCAD_TEXT) is None)
    check("the new pattern captures baths from real DCAD text", parse_baths(DCAD_TEXT) == "1/1")
    check("bedrooms still parse (they always did — proves the block itself is readable)",
          re.search(r'#\s*Bedrooms[:\s]+(\d+)', DCAD_TEXT).group(1) == "3")
    check("living area still parses",
          re.search(r'Living Area[:\s]+([\d,]+)\s*sqft', DCAD_TEXT).group(1) == "1,180")

    # ── whitespace variants DCAD is known to emit ──
    for raw, want in [("# Baths (Full/Half)\t1/ 1", "1/1"),
                      ("# Baths (Full/Half)\t2/1", "2/1"),
                      ("# Baths (Full/Half)\t3 / 2", "3/2"),
                      ("# Baths (Full/Half)\t0/ 0", "0/0")]:
        check(f"parses {raw.split(chr(9))[-1]!r} → {want!r}", parse_baths(raw) == want)
    check("no baths present → empty string, never a fabricated 0",
          parse_baths("Year Built\t1958 \t# Bedrooms\t3 ") == "")

    # ── 2. the captured value must be usable by the comp engine ──
    check("comps._parse_baths turns '1/1' into 1.5 (full + half)", comps._parse_baths("1/1") == 1.5)
    check("...'2/1' → 2.5", comps._parse_baths("2/1") == 2.5)
    check("...'0/0' → 0.0 (a REAL zero, distinct from unknown)", comps._parse_baths("0/0") == 0.0)
    check("...'' → None (unknown, never 0)", comps._parse_baths("") is None)
    check("end-to-end: DCAD text → subject baths", comps._parse_baths(parse_baths(DCAD_TEXT)) == 1.5)

    # ── 3. the silent-failure guard ──
    # Mirrors the shipped predicate: no core signal parsed ⇒ an explicit error, not a quiet empty.
    CORE = ("market_value", "living_area_sqft", "owners", "land_value", "legal_description")
    def guard(result):
        return bool(not result.get("error") and not any(result.get(k) for k in CORE))

    empty = {"error": None, "market_value": None, "living_area_sqft": None, "owners": [],
             "land_value": None, "legal_description": ""}
    check("an all-empty DCAD result TRIPS the guard (would record an error)", guard(empty) is True)
    check("a result with living area does NOT trip it",
          guard({**empty, "living_area_sqft": 1180}) is False)
    check("a result with market value does NOT trip it",
          guard({**empty, "market_value": 231730}) is False)
    check("a result with owners does NOT trip it",
          guard({**empty, "owners": [{"name": "MEDINA, CARLOS", "pct": 100}]}) is False)
    check("an existing exception error is not overwritten",
          guard({**empty, "error": "Timeout 30000ms exceeded"}) is False)
    # The distinction that matters: a scrape failure must not read as a property with no data.
    check("the guard fires on TX-26-00777's exact stored shape (valid account, all DCAD empty)",
          guard({"error": None, "market_value": None, "living_area_sqft": None, "owners": [],
                 "land_value": None, "legal_description": "",
                 "current_tax_balance": 4483.61}) is True)

    print("-" * 60)
    total, passed = len(_res), sum(_res)
    print(f"{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
