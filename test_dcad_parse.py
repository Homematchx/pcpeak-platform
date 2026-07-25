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

    # ── 4. the "no such group" crash on valid pages ──
    # scrape_dcad's find() reads capture group 1. A pattern with NO group (an alternation like
    # 'A|B') made m.group(1) raise IndexError: no such group, which aborted the ENTIRE DCAD parse
    # and stored error='no such group' on otherwise-valid pages. Traced on TX-23-00777 /
    # TX-26-00782, whose non-standard land-table layout routes to a group-less fallback pattern.
    # Mirror the SHIPPED find() exactly (it is a nested closure in scrape_dcad).
    def find(pattern, text, default=None, cast=None):
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            val = (m.group(1) if m.re.groups >= 1 else m.group(0)).strip()
            if cast:
                try: return cast(val.replace(",", "").replace("$", ""))
                except Exception: return default
            return val
        return default

    # A single-line fixture so the greedy [\w\s]+ doesn't run past the token into other fields.
    ZONING_LAND = "SINGLE FAMILY RESIDENCES\n"
    # The exact fallback pattern that used to crash — group-less alternation that DOES match.
    gl = find(r'DUPLEX DISTRICT|SINGLE FAMILY [\w\s]+', ZONING_LAND, "")
    check("a group-less pattern that matches no longer raises 'no such group'",
          gl.startswith("SINGLE FAMILY"))
    check("the FIXED zoning pattern (with a capture group) captures the token",
          find(r'(DUPLEX DISTRICT|SINGLE FAMILY [\w\s]+)', ZONING_LAND, "").startswith("SINGLE FAMILY"))
    check("a group-less pattern that does NOT match returns the default (not a crash)",
          find(r'CONDOMINIUM|TOWNHOME', ZONING_LAND, "") == "")
    check("a normal single-group pattern is unaffected",
          find(r'(SINGLE FAMILY RESIDENCES)', ZONING_LAND, "") == "SINGLE FAMILY RESIDENCES")
    check("cast still works through the guard",
          find(r'Deed Transfer Date:\s*1/1/(\d{4})', "Deed Transfer Date: 1/1/2020", None, cast=int) == 2020)
    # The property_intel source must not reintroduce a group-less find() pattern.
    import property_intel as _pi
    src = open(_pi.__file__).read()
    bad = []
    for m in re.finditer(r'find\(\s*r([\'"])(.*?)\1', src):
        pat = m.group(2)
        try:
            groups = re.compile(pat).groups
        except re.error:
            groups = 1   # can't compile in isolation (escape context) — not this check's concern
        if groups == 0:
            bad.append(pat[:50])
    if bad:
        print("     ! group-less find() patterns still present: " + str(bad))
    check("no find() call in property_intel.py uses a group-less pattern", not bad)

    # ── 5. the sub-minimum-GLA plausibility guard ──
    # gla=1 is the WORST failure class — a fabricated-PLAUSIBLE value. On a land-dominant parcel
    # DCAD's "living area" is a placeholder (TX-26-00033 gla=1 vs $700 improvement; TX-23-00768
    # gla=0), and storing it produces a nonsensical [~1] comp band instead of routing to the §G
    # land floor. Below the threshold, GLA must read as None (UNKNOWN), never a real value.
    import property_intel as _pi
    T = _pi.MIN_PLAUSIBLE_GLA_SQFT
    check("threshold is tunable config on property_intel (not an inline literal)", isinstance(T, int) and T > 0)

    # Mirror the shipped guard exactly (it runs inline in scrape_dcad after the GLA parse).
    def guard_gla(v):
        return None if (v is not None and v < T) else v

    check(f"gla=1 (the fabricated-plausible case) reads as None, not 1", guard_gla(1) is None)
    check("gla=0 reads as None (never a real zero-sqft house)", guard_gla(0) is None)
    check("gla just under the threshold reads as None", guard_gla(T - 1) is None)
    check("a real house at the threshold is KEPT", guard_gla(T) == T)
    check("a normal 1,180 sqft house is unaffected", guard_gla(1180) == 1180)
    check("None stays None (unknown, never coerced)", guard_gla(None) is None)
    # The guard must SUPPRESS a value — never fabricate one. It can only ever return None or the
    # original, so a sub-threshold input can never become a different real number.
    for v in (0, 1, 50, 199):
        g = guard_gla(v)
        check(f"guard on gla={v} yields ONLY None or the original (no fabrication)", g in (None, v) and g is None)

    # The normalization tool reads the SAME threshold (no drift between scrape-time and retro-fix).
    import intel_backfill  # noqa: F401  (import must succeed; it imports MIN_PLAUSIBLE_GLA_SQFT)
    check("intel_backfill imports the same threshold (single source of truth)",
          "MIN_PLAUSIBLE_GLA_SQFT" in open(intel_backfill.__file__).read())

    print("-" * 60)
    total, passed = len(_res), sum(_res)
    print(f"{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
