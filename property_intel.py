"""
property_intel.py — PC Peak Property Intelligence Engine v2
SpaceX-grade property data capture from DCAD and Dallas ACT.

Captures everything visible on DCAD:
  - Valuation: market, land, improvement
  - Physical: sqft, beds/baths, year built, construction type,
    roof, foundation, exterior wall, AC, heating, depreciation
  - Land: dimensions, zoning, lot size, unit price
  - Ownership: multi-owner breakdown with % 
  - Exemptions, deed transfer date, desirability
  - Estimated taxes by jurisdiction

Captures everything from Dallas ACT:
  - Current balance, levy, prior year due
  - Payment history with payer names (reveals prior owners)
  - Tax by year (shows penalty acceleration)
"""

import asyncio
import re
import json
from datetime import datetime


# ── DCAD ACCOUNT RESOLUTION ───────────────────────────────────
# When the petition's account is missing/garbled, resolve the real 17-digit
# DCAD account from the property address (primary) or owner name (fallback).
# Safety rule: only return an account we're CONFIDENT about — a wrong account
# produces confidently-wrong enrichment, which is worse than none.

def _parse_property_address(addr):
    """'2305 Hillside Ln, Carrollton, TX 75007' -> ('2305','HILLSIDE','CARROLLTON')."""
    if not addr:
        return "", "", ""
    a = addr.upper()
    # house number, an OPTIONAL leading direction (E./W./N./S./NE...), then the
    # real street name. Without skipping the direction, "106 E. Harvard Dr" parsed
    # its street as "E" and the DCAD address search failed.
    m = re.match(
        r"\s*(\d+)\s+(?:(?:N|S|E|W|NE|NW|SE|SW|NORTH|SOUTH|EAST|WEST)\.?\s+)?([A-Z0-9]+)", a)
    num = m.group(1) if m else ""
    street = m.group(2) if m else ""
    city = ""
    cm = re.search(r"CITY OF ([A-Z][A-Z ]+?)(?:,|\s+DALLAS|\s+TX|\s+TEXAS|$)", a)
    if cm:
        city = cm.group(1).strip()
    else:
        parts = [p.strip() for p in a.split(",")]
        for i, pp in enumerate(parts):
            if i > 0 and re.search(r"\bTX\b|\bTEXAS\b", pp):
                city = re.sub(r"\bCOUNTY\b", "", parts[i - 1]).strip()
                break
    return num, street, city


def _dcad_owner_query(name):
    """Case defendant -> DCAD owner-search query 'LASTNAME FIRSTNAME'."""
    if not name:
        return ""
    raw = re.sub(r"\bA/K/A\b.*", "", name.upper())
    raw = re.sub(r"\b(ET AL|INDIVIDUALLY.*|AS INDEPENDENT.*|EXECUTOR.*|DECEASED)\b", "", raw)
    if "," in raw:
        last = raw.split(",")[0].strip()
        rest = raw.split(",")[1].strip()
        first = rest.split()[0] if rest.split() else ""
        return f"{last} {first}".strip()
    m = re.search(r"HEIRS.*OF\s+(.+)", raw)
    if m:
        toks = m.group(1).split()
        return f"{toks[-1]} {toks[0]}".strip() if len(toks) >= 2 else (toks[-1] if toks else "")
    toks = [t for t in re.sub(r"[.,]", " ", raw).split() if t not in ("JR", "SR", "II", "III", "IV")]
    return f"{toks[-1]} {toks[0]}" if len(toks) >= 2 else " ".join(toks)


async def _dcad_results(page):
    """Parse (account, row-text) pairs from a DCAD search results page."""
    return await page.evaluate(
        "(function(){var o=[];document.querySelectorAll('a').forEach(function(a){"
        "var m=(a.href||'').match(/AcctDetail\\w*\\.aspx\\?ID=([0-9A-Za-z]{17})/);"
        "if(m){var tr=a.closest('tr');o.push({acct:m[1],"
        "row:(tr?tr.innerText:a.innerText).replace(/\\s+/g,' ').trim()});}});return o;})()")


def _pick_residential(cands):
    """Prefer a real-property (non 99-prefix BPP) account when several share an address."""
    res = [c for c in cands if not c["acct"].startswith("99")]
    return res[0]["acct"] if len(res) == 1 else ""


async def _address_search(browser, num, street, city):
    """DCAD address search -> [{acct,row}] (row text carries the OWNER name)."""
    ctx = await browser.new_context(); p = await ctx.new_page()
    try:
        await p.goto("https://www.dallascad.org/SearchAddr.aspx", timeout=40000, wait_until="domcontentloaded")
        await p.wait_for_timeout(800)
        await p.fill("#txtAddrNum", num)
        await p.fill("#txtStName", street)
        if city:
            try: await p.select_option("#listCity", label=city)
            except Exception: pass
        await p.click("#cmdSubmit")
        await p.wait_for_timeout(2200)
        return await _dcad_results(p)
    except Exception:
        return []
    finally:
        await ctx.close()


async def _owner_search(browser, query):
    """DCAD owner search -> [{acct,row}] (row text carries the property ADDRESS)."""
    ctx = await browser.new_context(); p = await ctx.new_page()
    try:
        await p.goto("https://www.dallascad.org/SearchOwner.aspx", timeout=40000, wait_until="domcontentloaded")
        await p.wait_for_timeout(800)
        await p.fill("#txtOwnerName", query)
        await p.click("#cmdSubmit")
        await p.wait_for_timeout(2200)
        return await _dcad_results(p)
    except Exception:
        return []
    finally:
        await ctx.close()


# ── Corroboration guard ───────────────────────────────────────
# The audit (n=92) showed DCAD's address search can return a CONFIDENTLY WRONG parcel
# (~2%). So an account is only trustworthy enough to write when a second, independent
# signal agrees. A wrong-but-confident account is worse than a delayed one.
_ESTATE_MARKERS = ("HEIRS", "ESTATE OF", "EST OF", " EST ", "ESTATE", "DECEASED",
                   "DEVISEE", "LIFE ESTATE", "UNKNOWN OWNER", "UNKNOWN HEIR")
_NAME_STOP = {"JR", "SR", "II", "III", "IV", "ET", "AL", "AKA", "A/K/A", "AND", "THE",
              "LIFE", "ESTATE", "OF", "EST", "HEIRS", "HEIR", "DECEASED", "DEVISEE",
              "INDIVIDUALLY", "ETUX", "ETVIR", "TRUSTEE", "TRUST", "&", "DBA"}

def _is_estate_case(defendant, extra=""):
    """Estate/heir case: the DCAD owner legitimately differs from the named defendant
    (heirs, deceased owner), so an owner-name mismatch is EXPECTED, not a red flag."""
    blob = ((defendant or "") + " " + (extra or "")).upper()
    return any(m in blob for m in _ESTATE_MARKERS)

def _name_tokens(name):
    raw = re.sub(r"[.,/]", " ", (name or "").upper())
    return {t for t in raw.split() if len(t) >= 3 and t not in _NAME_STOP}

def _owner_matches_defendant(owner_text, defendant):
    return bool(_name_tokens(owner_text) & _name_tokens(defendant))

def _owner_is_estate(owner_text):
    return any(m in (owner_text or "").upper() for m in _ESTATE_MARKERS)


async def resolve_account_corroborated(property_address, defendant, browser, extra_names=""):
    """Resolve a DCAD account ONLY when corroborated. Returns (account, confidence, reason):
       'corroborated'  — safe to auto-assign (two independent signals agree)
       'uncorroborated'— a candidate exists but only ONE unverified signal → do NOT write
       'unresolved'    — no candidate at all

    Corroboration paths (any one is sufficient):
      1. Address search and owner search independently return the SAME account.
      2. An address-search account whose row OWNER matches the defendant name.
      3. An owner-search account whose row ADDRESS contains the petition house#+street.
      4. Estate/heir case only: an address-search account whose owner shows estate markers
         (heirs/deceased/life estate) — owner won't match the heir defendant, but an estate
         owner AT the searched address corroborates it's the right parcel."""
    num, street, city = _parse_property_address(property_address)
    estate = _is_estate_case(defendant, extra_names)

    addr_cands = await _address_search(browser, num, street, city) if (num and street) else []
    q = _dcad_owner_query(defendant)
    owner_cands = await _owner_search(browser, q) if q else []

    def _res(accts):  # drop BPP (99-prefix) personal-property accounts
        return [a for a in accts if not a.startswith("99")]

    addr_accts = _res({c["acct"] for c in addr_cands})
    owner_accts = _res({c["acct"] for c in owner_cands})

    # 1) independent agreement — strongest
    both = set(addr_accts) & set(owner_accts)
    if len(both) == 1:
        return both.pop(), "corroborated", "address+owner agree"

    # 2) address result whose owner matches the defendant (non-estate path)
    for c in addr_cands:
        if not c["acct"].startswith("99") and _owner_matches_defendant(c["row"], defendant):
            return c["acct"], "corroborated", "address result; DCAD owner matches defendant"

    # 3) owner result whose address matches the petition address
    if num and street:
        for c in owner_cands:
            r = c["row"].upper()
            if not c["acct"].startswith("99") and num in r and (" " + street) in (" " + r):
                return c["acct"], "corroborated", "owner result; DCAD address matches petition"

    # 4) estate/heir: address result whose owner carries estate markers
    if estate:
        for c in addr_cands:
            if not c["acct"].startswith("99") and _owner_is_estate(c["row"]):
                return c["acct"], "corroborated", "estate case; address result has estate owner"

    # A candidate may exist but nothing corroborates it — do NOT write it.
    guess = (addr_accts[0] if len(addr_accts) == 1 else
             owner_accts[0] if len(owner_accts) == 1 else "")
    if guess:
        src = "address-only" if len(addr_accts) == 1 else "owner-only"
        return guess, "uncorroborated", src + " candidate; owner<->defendant not corroborated"
    return "", "unresolved", "no DCAD candidate found"


def corroboration_strength(reason):
    """Stable, machine-readable strength code for a corroborated resolution reason,
    so resolutions are queryable by HOW they were corroborated (revisit the weaker
    single-token 'owner-name' path once there's real volume):
      agreement    — two independent DCAD searches returned the same account (strongest)
      address-match— an owner-search result whose DCAD address matches the petition
      owner-name   — an address result whose owner shares a name token with the defendant
                     (the single-shared-token risk lives here)
      estate       — estate/heir case, address result with an estate owner
      other        — anything else (shouldn't happen for a corroborated result)"""
    r = (reason or "").lower()
    if "address+owner agree" in r:                     return "agreement"
    if "address matches petition" in r:                return "address-match"
    if "owner matches defendant" in r:                 return "owner-name"
    if "estate" in r:                                  return "estate"
    return "other"


async def resolve_dcad_account(property_address, owner_name, browser):
    """Resolve a 17-digit DCAD account. Address search first (targets the exact
    parcel), owner search as fallback. Returns (account, how) — account is "" if
    we can't resolve it confidently."""
    num, street, city = _parse_property_address(property_address)
    # 1) Address search — the precise path.
    if num and street:
        cands = await _address_search(browser, num, street, city)
        if len(cands) == 1:
            return cands[0]["acct"], "address-sole"
        if len(cands) > 1:
            pick = _pick_residential(cands)
            if pick:
                return pick, "address-residential"
            # otherwise ambiguous — don't guess

    # 2) Owner search — fallback; only a sole result or an address-matched one.
    q = _dcad_owner_query(owner_name)
    if q:
        cands = await _owner_search(browser, q)
        if len(cands) == 1:
            return cands[0]["acct"], "owner-sole"
        if len(cands) > 1 and num and street:
            for c in cands:
                if num in c["row"] and street in c["row"].upper():
                    return c["acct"], "owner-addr-match"
    return "", "unresolved"


async def scrape_dcad(account_number: str, browser) -> dict:
    result = {
        # Valuation
        "market_value": None,
        "land_value": None,
        "improvement_value": None,
        # Physical characteristics
        "year_built": None,
        "effective_year_built": None,
        "actual_age": None,
        "building_class": "",
        "construction_type": "",
        "foundation": "",
        "roof_type": "",
        "roof_material": "",
        "exterior_wall": "",
        "living_area_sqft": None,
        "total_area_sqft": None,
        "bedrooms": None,
        "bathrooms": "",
        "stories": "",
        "depreciation_pct": None,
        "desirability": "",
        "air_condition": "",
        "heating": "",
        "garage_sqft": None,
        # Land
        "zoning": "",
        "lot_frontage_ft": None,
        "lot_depth_ft": None,
        "lot_area_sqft": None,
        "land_unit_price": None,
        # Ownership
        "legal_description": "",
        "owners": [],
        "deed_transfer_date": "",
        "exemptions": "",
        "no_homestead": False,
        # Estimated taxes
        "estimated_annual_taxes": None,
        "tax_rates": [],
        # Meta
        "dcad_url": f"https://www.dallascad.org/AcctDetailRes.aspx?ID={account_number}",
        "error": None
    }

    page = None
    try:
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        await page.goto(
            f"https://www.dallascad.org/AcctDetailRes.aspx?ID={account_number}",
            wait_until="domcontentloaded", timeout=30000
        )
        # DCAD's valuation block can finish rendering slightly after
        # domcontentloaded, especially when several tract pages load
        # concurrently (multi-tract petitions). A single fixed-delay read then
        # silently misses market_value. Poll until the valuation line appears.
        text = ""
        for _ in range(5):
            await asyncio.sleep(1.5)
            text = await page.inner_text("body")
            if re.search(r'Market Value:\s*\$[\d,]+\s*\+\s*\$[\d,]+\s*=\s*\$[\d,]+', text):
                break

        def find(pattern, default=None, cast=None):
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                if cast:
                    try: return cast(val.replace(",","").replace("$",""))
                    except: return default
                return val
            return default

        # Valuation — exact DCAD format:
        # "Improvement:\nLand:\nMarket Value:\t$49,350\n+ $109,480\n=$158,830"
        # improvement = first dollar amount after "Market Value:\t"
        # land = amount after "+ $"
        # market = amount after "="
        val_section = re.search(
            r'Market Value:\s*\$([\d,]+)\s*\+\s*\$([\d,]+)\s*=\s*\$([\d,]+)',
            text
        )
        if val_section:
            result["improvement_value"] = int(val_section.group(1).replace(",",""))
            result["land_value"] = int(val_section.group(2).replace(",",""))
            result["market_value"] = int(val_section.group(3).replace(",",""))
        else:
            # Fallback
            mv_m = re.search(r'=\$([\d,]+)', text)
            if mv_m:
                result["market_value"] = int(mv_m.group(1).replace(",",""))
        # Taxable value — first value in Estimated Taxes table
        tax_val_m = re.search(r'Taxable Value\s+\$([\d,]+)', text)
        if tax_val_m:
            result["taxable_value"] = int(tax_val_m.group(1).replace(",",""))

        # Physical
        result["year_built"] = find(r'Year Built[:\s]+(\d{4})', cast=int)
        result["effective_year_built"] = find(r'Effective Year Built[:\s]+(\d{4})', cast=int)
        result["actual_age"] = find(r'Actual Age[:\s]+(\d+)', cast=int)
        result["building_class"] = find(r'Building Class[:\s]+(\w+)', "")
        # Physical chars use tabs: "Building Class\t04\tConstruction Type\tFRAME\t..."
        result["construction_type"] = find(r'Construction Type\t([A-Z\s]+?)(?:\xa0|\t)', "")
        result["foundation"] = find(r'Foundation\t([A-Z\s]+?)(?:\xa0|\t)', "")
        result["roof_type"] = find(r'Roof Type\t([A-Z\s]+?)(?:\xa0|\t)', "")
        result["roof_material"] = find(r'Roof Material\t([A-Z\s]+?)(?:\xa0|\t)', "")
        result["exterior_wall"] = find(r'Ext\.? Wall Material\t([A-Z\s]+?)(?:\xa0|\t)', "")
        result["roof_type"] = find(r'Roof Type[:\s]+([A-Z/\s]+?)(?:\n|Roof Material)', "")
        result["roof_material"] = find(r'Roof Material[:\s]+([A-Z\s]+?)(?:\n|Fence)', "")
        result["exterior_wall"] = find(r'Ext\.? Wall Material[:\s]+([A-Z\s]+?)(?:\n|Basement|Heating)', "")
        result["living_area_sqft"] = find(r'Living Area[:\s]+([\d,]+)\s*sqft', cast=int)
        result["total_area_sqft"] = find(r'Total Area[:\s]+([\d,]+)\s*sqft', cast=int)
        result["bedrooms"] = find(r'#\s*Bedrooms[:\s]+(\d+)', cast=int)
        # BATHS: DCAD renders "# Baths (Full/Half)\t1/ 1" — note the SPACE after the slash. The old
        # pattern (\d+/\d+) required the digits to be adjacent to it, so it NEVER matched and every
        # case stored "" (measured: 0 of 247 enriched cases had baths, while 164 had bedrooms —
        # the label parsed, the value didn't). Baths feed the comp MatchScore (beds_baths weight),
        # so this silently degraded every ARV match. Tolerate whitespace and normalize to "F/H",
        # which is what comps._parse_baths already expects ("1/1" → 1.5).
        _b = re.search(r'#\s*Baths[^\d]*(\d+)\s*/\s*(\d+)', text)
        result["bathrooms"] = f"{_b.group(1)}/{_b.group(2)}" if _b else ""
        result["stories"] = find(r'#\s*Stories[:\s]+([A-Z\s]+?)(?:\n|Depreciation)', "")
        dep = find(r'Depreciation[:\s]+(\d+)%', cast=int)
        result["depreciation_pct"] = dep
        result["desirability"] = find(r'Desirability[:\s]+([A-Z]+)', "")
        result["air_condition"] = find(r'Air Condition(?:ing)?[:\s]+([A-Z\s]+?)(?:\n|Pool|Spa)', "")
        result["heating"] = find(r'Heating[:\s]+([A-Z\s]+?)(?:\n|Air)', "")

        # Garage
        garage = re.search(r'DETACHED GARAGE[^\d]+([\d,]+)', text, re.IGNORECASE)
        if garage:
            result["garage_sqft"] = int(garage.group(1).replace(",",""))

        # Land dimensions
        # Land table uses TABS:
        # "1\tSINGLE FAMILY RESIDENCES\tDUPLEX DISTRICT\t85\t92\t7,820.0000 SQUARE FEET\tSTANDARD\t$14.00\t0%\t$109,480\tN"
        land_row = re.search(
            r'1\t([^\t]+)\t([^\t]+)\t(\d+)\t(\d+)\t([\d,\.]+) SQUARE FEET\t[^\t]+\t\$([\d,\.]+)\t[^\t]+\t\$([\d,]+)',
            text
        )
        if land_row:
            result["zoning"] = land_row.group(2).strip()
            result["lot_frontage_ft"] = int(land_row.group(3))
            result["lot_depth_ft"] = int(land_row.group(4))
            result["lot_area_sqft"] = float(land_row.group(5).replace(",",""))
            result["land_unit_price"] = float(land_row.group(6).replace(",",""))
            result["adjusted_land_price"] = int(land_row.group(7).replace(",",""))
        else:
            # Fallback
            result["zoning"] = find(r'DUPLEX DISTRICT|SINGLE FAMILY [\w\s]+', "")
            result["lot_frontage_ft"] = find(r'Frontage \(ft\)[\s\t]+(\d+)', cast=int)
            result["lot_depth_ft"] = find(r'Depth \(ft\)[\s\t]+(\d+)', cast=int)
            lot_area = find(r'([\d,]+\.?\d*)\s*SQUARE FEET', cast=float)
            if lot_area:
                result["lot_area_sqft"] = lot_area

        # Deed transfer
        result["deed_transfer_date"] = find(r'Deed Transfer Date[:\s]+([\d/]+)', "")

        # Exemptions
        ex_text = find(r'Exemptions?\s*\(2026[^\)]*\)[^\n]*\n([^\n]+)', "")
        if not ex_text:
            ex_text = find(r'Exemptions?[:\s]+([A-Z,\s]+?)(?:\n\n|\nEstimated)', "")
        if "No Exemption" in text or "No Homestead" in text:
            result["no_homestead"] = True
            result["exemptions"] = "None"
        else:
            result["exemptions"] = ex_text or ""
            result["no_homestead"] = "HMSTD" not in text and "Homestead" not in text

        # Legal description
        legal_lines = re.search(r'Legal Desc[^\n]*\n((?:[^\n]+\n){1,6})', text)
        if legal_lines:
            result["legal_description"] = " ".join(legal_lines.group(1).split())

        # Ownership breakdown
        owner_section = re.search(r'Multi-Owner[^\n]*\n(.*?)(?:\n\n|Main Improvement)', text, re.DOTALL)
        if owner_section:
            owner_rows = re.findall(r'([A-Z][A-Z\s&,\.]+?)\s+(\d{1,3})%', owner_section.group(1))
            for name, pct in owner_rows:
                name = name.strip()
                if len(name) > 3:
                    result["owners"].append({"name": name, "pct": int(pct)})

        # Estimated taxes
        total_est = find(r'Total Estimated Taxes[:\s]+\$?([\d,\.]+)', cast=float)
        result["estimated_annual_taxes"] = total_est

        # Tax rates by jurisdiction
        rate_rows = re.findall(r'(DALLAS[A-Z\s]*|PARKLAND[A-Z\s]*|UNASSIGNED)\s+\$([\d,\.]+)', text)
        for entity, amount in rate_rows[:6]:
            result["tax_rates"].append({"entity": entity.strip(), "estimated_tax": float(amount.replace(",",""))})

        # SILENT-FAILURE GUARD. Everything above only records an error when an EXCEPTION is raised.
        # A DCAD page that loads but renders nothing parsable (render timeout, session hiccup, a
        # retired/merged account) therefore returned all-empty fields with error=None — which is
        # indistinguishable from "this property genuinely has no data". That is exactly the
        # conflation the project standard forbids: an unknown must never look like a real answer.
        # Measured on TX-26-00777 (5807 Morningside Ave — a VALID 17-digit account whose DCAD page
        # DOES carry 1,180 sqft / 3 bed / 1-1 bath): every DCAD field empty, errors.dcad = None, so
        # the case looked enriched and propose 422'd for "no living area" with no way to tell a
        # scrape failure from a real data gap. If NO core signal parsed, say so.
        if not result.get("error") and not any(result.get(k) for k in
                                               ("market_value", "living_area_sqft", "owners",
                                                "land_value", "legal_description")):
            result["error"] = (f"DCAD returned no parsable data for account {account_number} "
                               f"(page loaded, {len(text or '')} chars) — re-scrape needed; "
                               f"NOT evidence that the property has no data")

    except Exception as e:
        result["error"] = str(e)
    finally:
        if page:
            try:
                await page.close()
                await context.close()
            except Exception:
                pass

    return result


async def scrape_dallas_act(account_number: str, browser) -> dict:
    result = {
        "current_balance": None,
        "current_levy": None,
        "prior_year_due": None,
        "total_amount_due": None,
        "payment_history": [],
        "tax_by_year": [],
        "act_url": f"https://www.dallasact.com/act_webdev/dallas/showdetail2.jsp?can={account_number}&ownerno=0",
        "error": None
    }

    page = None
    try:
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Main detail page
        await page.goto(
            f"https://www.dallasact.com/act_webdev/dallas/showdetail2.jsp?can={account_number}&ownerno=0",
            wait_until="domcontentloaded", timeout=30000
        )
        await asyncio.sleep(2)
        text = await page.inner_text("body")

        def find(pattern, cast=None):
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                val = m.group(1).strip().replace(",","").replace("$","")
                if cast:
                    try: return cast(val)
                    except: return None
            return None

        result["current_levy"] = find(r'Current Tax Levy[:\s]+\$?([\d,\.]+)', cast=float)
        result["current_balance"] = find(r'Current Amount Due[:\s]+\$?([\d,\.]+)', cast=float)
        result["prior_year_due"] = find(r'Prior Year Amount Due[:\s]+\$?([\d,\.]+)', cast=float)
        result["total_amount_due"] = find(r'Total Amount Due[:\s]+\$?([\d,\.]+)', cast=float)

        # Payment history
        await page.goto(
            f"https://www.dallasact.com/act_webdev/dallas/reports/paymentinfo.jsp?can={account_number}&ownerno=0",
            wait_until="domcontentloaded", timeout=30000
        )
        await asyncio.sleep(2)
        text = await page.inner_text("body")

        # The ACT payment page renders each field on its OWN line (date, then amount,
        # then optional tax-year / payer) — NOT single-line rows — and many payments
        # (older ones especially) have no payer/year at all. The old single-line,
        # payer-REQUIRED regex dropped those rows and capped at 15 (verified: acct
        # 00000219142000000 has 7-8 payments, stored 1). Same table-layout bug class as
        # the DCAD ownership-history fix. Pair each payment date with the $amount that
        # follows it; capture year/payer only when present; do not require them; no cap.
        DATE = re.compile(r'^(\d{4}-\d{2}-\d{2})$')
        AMT  = re.compile(r'^\$?([\d,]+\.\d{2})$')
        YEAR = re.compile(r'^\d{4}(?:[,\s]+\d{4})*$')
        _HEADERS = {"PAYMENT AMOUNT", "TAX YEAR PAID", "PAYMENT INFORMATION"}
        cur = None
        pays = []
        for l in (ln.strip() for ln in text.splitlines() if ln.strip()):
            dm = DATE.match(l)
            if dm:
                cur = {"date": dm.group(1), "amount": None, "tax_year": "", "payer": ""}
                pays.append(cur)
                continue
            if cur is None:
                continue
            am = AMT.match(l)
            if am and cur["amount"] is None:
                try: cur["amount"] = float(am.group(1).replace(",", ""))
                except Exception: pass
            elif YEAR.match(l) and not cur["tax_year"]:
                cur["tax_year"] = l
            elif re.search(r"[A-Za-z]", l) and not cur["payer"] and l.upper() not in _HEADERS:
                cur["payer"] = l[:50]
        # keep only real payment rows (a date paired with an amount)
        result["payment_history"] = [p for p in pays if p["amount"] is not None][:100]

        # Tax by year
        await page.goto(
            f"https://www.dallasact.com/act_webdev/dallas/reports/taxbyyear.jsp?can={account_number}&ownerno=0",
            wait_until="domcontentloaded", timeout=30000
        )
        await asyncio.sleep(2)
        text = await page.inner_text("body")

        for line in text.splitlines():
            yr_m = re.match(r'\s*(\d{4})\s+\$([\d,\.]+)\s+\$([\d,\.]+)\s+\$([\d,\.]+)', line)
            if yr_m:
                try:
                    result["tax_by_year"].append({
                        "year": int(yr_m.group(1)),
                        "base_tax": float(yr_m.group(2).replace(",","")),
                        "penalty_interest": float(yr_m.group(3).replace(",","")),
                        "total": float(yr_m.group(4).replace(",",""))
                    })
                except Exception:
                    pass

    except Exception as e:
        result["error"] = str(e)
    finally:
        if page:
            try:
                await page.close()
                await context.close()
            except Exception:
                pass

    return result


def analyze_distress(dcad: dict, act: dict) -> dict:
    signals = []
    score = 0

    # Depreciation
    dep = dcad.get("depreciation_pct") or 0
    if dep >= 50:
        signals.append({"type": "high_depreciation", "label": f"Structure {dep}% depreciated — near end of economic life", "severity": "critical"})
        score += 3
    elif dep >= 30:
        signals.append({"type": "depreciation", "label": f"Structure {dep}% depreciated — significant wear", "severity": "warning"})
        score += 1

    # Asbestos/hazmat exterior
    ext = (dcad.get("exterior_wall") or "").upper()
    if "ASBESTOS" in ext:
        signals.append({"type": "hazmat", "label": "Asbestos shingles — remediation cost risk", "severity": "critical"})
        score += 2

    # Age
    age = dcad.get("actual_age") or 0
    if age >= 80:
        signals.append({"type": "age", "label": f"Structure {age} years old — major systems near end of life", "severity": "warning"})
        score += 1

    # No exemptions = likely not owner-occupied
    if dcad.get("no_homestead"):
        signals.append({"type": "no_homestead", "label": "No homestead exemption — not owner-occupied or absentee", "severity": "warning"})
        score += 1

    # Payment gap
    payments = act.get("payment_history", [])
    if payments:
        try:
            last_year = int(sorted(payments, key=lambda x: x["date"], reverse=True)[0]["date"][:4])
            gap = datetime.now().year - last_year
            if gap >= 3:
                signals.append({"type": "payment_gap", "label": f"No tax payment for {gap} years — owner disengaged", "severity": "critical"})
                score += 3
            elif gap >= 1:
                signals.append({"type": "payment_gap", "label": f"Last payment {last_year} — {gap} year gap", "severity": "warning"})
                score += 1
        except Exception:
            pass

    # Multiple owners
    owners = dcad.get("owners", [])
    if len(owners) > 2:
        signals.append({"type": "split_title", "label": f"{len(owners)}-way ownership split — complex title negotiation", "severity": "warning"})
        score += 2

    # Recent deed transfer
    deed = dcad.get("deed_transfer_date", "")
    if deed:
        try:
            yr = int(deed.split("/")[-1])
            if yr >= 2023:
                signals.append({"type": "recent_transfer", "label": f"Deed transferred {deed} — ownership change, may be motivated", "severity": "info"})
                score += 1
        except Exception:
            pass

    # Penalty acceleration
    tax_years = act.get("tax_by_year", [])
    if tax_years:
        latest = max(tax_years, key=lambda x: x["year"])
        if latest["base_tax"] > 0:
            pct = latest["penalty_interest"] / latest["base_tax"] * 100
            if pct > 60:
                signals.append({"type": "penalty", "label": f"Penalties {pct:.0f}% of base tax — severe delinquency acceleration", "severity": "critical"})
                score += 2

    # Improvement vs land ratio
    imp = dcad.get("improvement_value") or 0
    land = dcad.get("land_value") or 1
    if imp < 5000:
        signals.append({"type": "vacant", "label": "Improvement value near zero — likely vacant lot or demolished", "severity": "critical"})
        score += 3
    elif imp < land * 0.2:
        signals.append({"type": "distressed", "label": "Structure value well below land value — teardown candidate", "severity": "warning"})
        score += 1

    return {
        "score": score,
        "level": "critical" if score >= 6 else "high" if score >= 4 else "moderate" if score >= 2 else "low",
        "signals": signals
    }


def _city_of(address: str) -> str:
    """Extract the city from a property address, e.g.
    '3625 Crane St, Dallas, TX 75212' -> 'Dallas'."""
    if not address:
        return ""
    parts = [p.strip() for p in address.split(",")]
    for i, p in enumerate(parts):
        if i > 0 and re.search(r"\b(TX|TEXAS)\b", p, re.I):
            return parts[i - 1]
    return parts[1] if len(parts) > 1 else ""


def _derive_owner_signals(ownership_history: list, property_address: str = "") -> dict:
    """Derive owner_changes / is_absentee / estate_flag from an ownership_history
    list (stored newest-first). Pure function of its inputs so it can be recomputed
    from stored data without re-scraping.

    owner_changes iterates oldest -> newest so from/to and year reflect the real
    direction of each transfer (the newest-first list would otherwise report every
    change backwards)."""
    changes = []
    prev = None
    for rec in reversed(ownership_history):
        owner = rec.get("owner", "")
        if prev and owner != prev.get("owner", ""):
            changes.append({
                "year": rec.get("year"),
                "from_owner": prev.get("owner", ""),
                "to_owner": owner,
                "deed_date": rec.get("deed_transfer_date", ""),
                "flags": _analyze_owner_change(prev.get("owner", ""), owner),
            })
        prev = rec

    # Absentee: latest owner mails to a different city than the property.
    is_absentee = False
    if ownership_history:
        mail_token = (ownership_history[0].get("mailing_city") or "").upper().split(",")[0].strip()
        prop_city = _city_of(property_address).upper().strip()
        if mail_token and prop_city:
            is_absentee = mail_token not in prop_city and prop_city not in mail_token
        elif mail_token:
            # No property city to compare; this is a Dallas-county tool, so an
            # owner mailing outside Dallas is treated as absentee.
            is_absentee = mail_token != "DALLAS"

    estate = bool(re.search(r"ESTATE|HEIR|DEVISEE",
                            " ".join(r.get("owner", "") for r in ownership_history), re.I))
    return {
        "owner_changes": changes,
        "is_absentee": is_absentee,
        "mailing_differs_from_property": is_absentee,
        "estate_flag": estate,
    }


async def scrape_dcad_history(account_number: str, browser, property_address: str = "") -> dict:
    result = {
        "ownership_history": [],
        "owner_changes": [],
        "market_value_history": [],
        "taxable_value_history": [],
        "exemptions_history": [],
        "is_absentee": False,
        "estate_flag": False,
        "mailing_differs_from_property": False,
        "history_url": "https://www.dallascad.org/AcctHistory.aspx?ID=" + account_number,
        "error": None
    }
    page = None
    try:
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page = await context.new_page()
        await page.goto(
            "https://www.dallascad.org/AcctHistory.aspx?ID=" + account_number,
            wait_until="domcontentloaded", timeout=30000
        )
        await asyncio.sleep(2)
        text = await page.inner_text("body")

        # AcctHistory.aspx stacks FOUR year-indexed tables that ALL start rows
        # with "YEAR\t":  Owner/Legal Description, Market Value, Taxable Value,
        # and Exemptions. Splitting the whole page on year boundaries merged
        # every table's rows into ownership_history (duplicate + junk "owners"
        # like "$0", "No Exemptions"). Slice the page into its sections by their
        # header lines FIRST, then parse each table on its own.
        def _slice(start, ends):
            si = text.find(start)
            if si == -1:
                return ""
            si += len(start)
            cand = [c for c in (text.find(e, si) for e in ends) if c != -1]
            return text[si:min(cand)] if cand else text[si:]

        # Residential accounts head the ownership table with
        # "Year\tOwner\tLegal Description"; Business Personal Property
        # (99-prefix) accounts use "Year\tLegal Owner\tDoing Business As (DBA)".
        # Accept whichever is present.
        owner_hdr = "Year\tOwner\tLegal Description"
        if owner_hdr not in text:
            owner_hdr = "Year\tLegal Owner\tDoing Business As (DBA)"
        owner_sec = _slice(owner_hdr,
                           ["Market Value", "Taxable Value", "\nExemptions", "Exemption Details History"])
        market_sec = _slice("Year\tImprovement\tLand",
                            ["Taxable Value", "\nExemptions", "Exemption Details History"])
        taxable_sec = _slice("Year\tCity\tISD",
                             ["\nExemptions", "Exemption Details History"])
        exempt_sec = _slice("\nExemptions\n",
                            ["Exemption Details History", "© 20"])

        # --- Ownership / Legal Description table (the real ownership history) ---
        for block in re.split(r"(?=\n[0-9]{4}\t)", "\n" + owner_sec):
            yr_m = re.match(r"\n?([0-9]{4})\t([^\n]+)", block)
            if not yr_m:
                continue

            year = int(yr_m.group(1))
            owner_name = yr_m.group(2).strip()

            # Mailing address + city are the two lines after the year/owner line.
            lines_b = block.split("\n")
            yi = next((k for k, l in enumerate(lines_b) if re.match(r"[0-9]{4}\t", l)), 0)
            mailing_addr = lines_b[yi + 1].replace("\xa0", " ").strip() if yi + 1 < len(lines_b) else ""
            mailing_city = lines_b[yi + 2].replace("\xa0", " ").strip() if yi + 2 < len(lines_b) else ""

            # Deed transfer date
            deed_m = re.search(r"Deed Transfer Date:[\xa0\s]+(\d+/\d+/\d+)", block)
            deed_date = deed_m.group(1) if deed_m else ""

            record = {
                "year": year,
                "owner": owner_name,
                "mailing_address": mailing_addr,
                "mailing_city": mailing_city,
                "deed_transfer_date": deed_date
            }
            result["ownership_history"].append(record)

        # --- Value / exemption tables, each into its own list (not ownership) ---
        def _year_table(sec, cols):
            out = []
            for line in sec.splitlines():
                m = re.match(r"([0-9]{4})\t(.+)", line)
                if not m:
                    continue
                vals = [v.strip() for v in m.group(2).split("\t")]
                rec = {"year": int(m.group(1))}
                for idx, name in enumerate(cols):
                    rec[name] = vals[idx] if idx < len(vals) else ""
                out.append(rec)
            return out

        result["market_value_history"] = _year_table(
            market_sec, ["improvement", "land", "total_market", "homestead_capped"])
        result["taxable_value_history"] = _year_table(
            taxable_sec, ["city", "isd", "county", "college", "hospital", "special_district"])
        result["exemptions_history"] = _year_table(exempt_sec, ["exemptions"])

        # Derived owner signals: chronological owner_changes, absentee, estate.
        result.update(_derive_owner_signals(result["ownership_history"], property_address))

    except Exception as e:
        result["error"] = str(e)
    finally:
        if page:
            try:
                await page.close()
                await context.close()
            except Exception:
                pass
    return result


def _analyze_owner_change(from_owner: str, to_owner: str) -> list:
    """Analyze what an ownership change means."""
    flags = []
    from_up = from_owner.upper()
    to_up = to_owner.upper()
    
    if "ESTATE OF" in to_up or "HEIR" in to_up:
        flags.append("DECEASED — estate transfer")
    if "ESTATE OF" in from_up and "ESTATE OF" not in to_up:
        flags.append("Estate resolved — new owner")
    if any(w in to_up for w in ["TRUST", "LLC", "INC", "CORP"]):
        flags.append("Transferred to entity — investor/trust")
    if "FEDERAL TITLE" in to_up or "TITLE" in to_up:
        flags.append("Title company involved — active sale")
    if from_up.split()[0] == to_up.split()[0]:  # same last name
        flags.append("Family transfer — same surname")
    if not flags:
        flags.append("Arm's length sale")
    return flags


async def _enrich_single_account(account_number: str, address: str, browser,
                                  gsv_api_key: str = None) -> dict:
    if not account_number or len(account_number) < 10:
        return {}

    print(f"  [intel] Enriching {account_number}...")

    dcad, act, history = await asyncio.gather(
        scrape_dcad(account_number, browser),
        scrape_dallas_act(account_number, browser),
        scrape_dcad_history(account_number, browser, address)
    )

    distress = analyze_distress(dcad, act)

    # Street view static URL (no API key needed for embed)
    import urllib.parse
    sv_url = f"https://maps.googleapis.com/maps/api/streetview?size=640x400&location={urllib.parse.quote(address or '')}&fov=90&key=AIzaSyD-placeholder" if address else ""

    result = {
        "account_number": account_number,
        # Valuation
        "market_value": dcad.get("market_value"),
        "land_value": dcad.get("land_value"),
        "improvement_value": dcad.get("improvement_value"),
        "estimated_annual_taxes": dcad.get("estimated_annual_taxes"),
        # Physical
        "year_built": dcad.get("year_built"),
        "effective_year_built": dcad.get("effective_year_built"),
        "actual_age": dcad.get("actual_age"),
        "building_class": dcad.get("building_class"),
        "construction_type": dcad.get("construction_type"),
        "foundation": dcad.get("foundation"),
        "roof_type": dcad.get("roof_type"),
        "roof_material": dcad.get("roof_material"),
        "exterior_wall": dcad.get("exterior_wall"),
        "living_area_sqft": dcad.get("living_area_sqft"),
        "total_area_sqft": dcad.get("total_area_sqft"),
        "bedrooms": dcad.get("bedrooms"),
        "bathrooms": dcad.get("bathrooms"),
        "stories": dcad.get("stories"),
        "depreciation_pct": dcad.get("depreciation_pct"),
        "desirability": dcad.get("desirability"),
        "air_condition": dcad.get("air_condition"),
        "heating": dcad.get("heating"),
        "garage_sqft": dcad.get("garage_sqft"),
        # Land
        "zoning": dcad.get("zoning"),
        "lot_frontage_ft": dcad.get("lot_frontage_ft"),
        "lot_depth_ft": dcad.get("lot_depth_ft"),
        "lot_area_sqft": dcad.get("lot_area_sqft"),
        "land_unit_price": dcad.get("land_unit_price"),
        # Ownership
        "legal_description": dcad.get("legal_description"),
        "owners": dcad.get("owners", []),
        "deed_transfer_date": dcad.get("deed_transfer_date"),
        "exemptions": dcad.get("exemptions"),
        "no_homestead": dcad.get("no_homestead"),
        "tax_rates": dcad.get("tax_rates", []),
        # ACT
        "current_tax_balance": act.get("total_amount_due"),
        "current_levy": act.get("current_levy"),
        "prior_year_due": act.get("prior_year_due"),
        "payment_history": act.get("payment_history", []),
        "tax_by_year": act.get("tax_by_year", []),
        # Analysis
        "distress": distress,
        "street_view_url": sv_url,
        "dcad_url": dcad.get("dcad_url"),
        "act_url": act.get("act_url"),
        # Ownership history
        "ownership_history": history.get("ownership_history", []),
        "owner_changes": history.get("owner_changes", []),
        "market_value_history": history.get("market_value_history", []),
        "taxable_value_history": history.get("taxable_value_history", []),
        "exemptions_history": history.get("exemptions_history", []),
        "is_absentee": history.get("is_absentee", False),
        "estate_flag": history.get("estate_flag", False),
        "mailing_differs_from_property": history.get("mailing_differs_from_property", False),
        "history_url": history.get("history_url", ""),
        "enriched_at": datetime.now().isoformat(),
        "errors": {"dcad": dcad.get("error"), "act": act.get("error")}
    }

    # 'unknown' (None = scrape miss) is DISTINCT from a real $0 (taxes paid). Use
    # `is None`, not falsy — 0.0 is a real value, not a missing one.
    mv = "unknown" if dcad.get("market_value") is None else f"${dcad.get('market_value'):,.0f}"
    bal = "unknown" if act.get("total_amount_due") is None else f"${act.get('total_amount_due'):,.2f}"
    print(f"  [intel] {account_number} | MV: {mv} | Balance: {bal} | Distress: {distress['level'].upper()}")

    return result


# Fields that represent a dollar amount or an area and can be correctly summed
# across multiple tracts on the same petition (e.g. a lot split into 4A/4C).
_SUMMABLE_FIELDS = [
    "market_value", "land_value", "improvement_value", "estimated_annual_taxes",
    "current_tax_balance", "current_levy", "prior_year_due", "lot_area_sqft",
]


def _aggregate_multi_tract(results: list, account_numbers: list) -> dict:
    """Combine per-tract enrich_property() results into one property-level
    record. Financial/area fields are summed; everything else (physical
    characteristics, ownership, etc.) is taken from the tract with the
    highest market value, since that's usually the improved/primary parcel."""
    valid = [r for r in results if r]
    if not valid:
        return {}

    primary = max(valid, key=lambda r: (r.get("market_value") or 0))
    combined = dict(primary)  # start from the primary tract, then overwrite sums below

    for field in _SUMMABLE_FIELDS:
        total = 0
        any_value = False
        for r in valid:
            v = r.get(field)
            if v is not None:
                total += v
                any_value = True
        combined[field] = total if any_value else None

    combined["account_number"] = ", ".join(account_numbers)
    combined["tract_count"] = len(valid)
    combined["tracts"] = valid  # full per-tract detail preserved for auditing
    combined["dcad_url"] = [r.get("dcad_url") for r in valid]
    combined["act_url"] = [r.get("act_url") for r in valid]
    # Re-run distress analysis using the primary tract's full physical/payment
    # data (analyze_distress doesn't key off raw dollar totals directly, so
    # this stays accurate even though the dollar fields above are now summed).
    combined["distress"] = analyze_distress(primary, primary)
    return combined


async def enrich_property(account_number: str, address: str, browser,
                           gsv_api_key: str = None) -> dict:
    """Public entry point. Handles both the normal single-account case and
    petitions that list multiple DCAD account numbers for one property
    (comma- or semicolon-separated). Previously a multi-account string was
    passed straight through as a single malformed ID — this splits it,
    enriches each tract independently, and sums the financial fields."""
    if not account_number:
        return {}

    accounts = [a.strip() for a in re.split(r"[,;]", account_number)
                if a.strip() and len(a.strip()) >= 10]
    if not accounts:
        return {}

    if len(accounts) == 1:
        return await _enrich_single_account(accounts[0], address, browser, gsv_api_key)

    print(f"  [intel] Multi-tract petition detected — {len(accounts)} accounts: {', '.join(accounts)}")
    # Enrich tracts sequentially, not concurrently: firing every tract's DCAD
    # requests in parallel (accounts x detail+history) throttles dallascad.org
    # and non-deterministically drops market_value on the throttled page. A
    # couple of extra seconds per tract buys reliable reads.
    results = []
    for a in accounts:
        results.append(await _enrich_single_account(a, address, browser, gsv_api_key))
    combined = _aggregate_multi_tract(list(results), accounts)

    mv = "unknown" if combined.get("market_value") is None else f"${combined.get('market_value'):,.0f}"
    bal = "unknown" if combined.get("current_tax_balance") is None else f"${combined.get('current_tax_balance'):,.2f}"
    dist = combined.get("distress", {}).get("level", "unknown")
    print(f"  [intel] {account_number} | {len(accounts)} tracts combined | MV: {mv} | Balance: {bal} | Distress: {dist.upper()}")

    return combined


async def main():
    import sys, json
    from playwright.async_api import async_playwright
    acct = sys.argv[1] if len(sys.argv) > 1 else "00000153766000000"
    addr = sys.argv[2] if len(sys.argv) > 2 else "1549 Harris Ct, Dallas TX 75223"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        intel = await enrich_property(acct, addr, browser)
        print(json.dumps(intel, indent=2))
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
