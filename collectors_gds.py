#!/usr/bin/env python3
"""GDS / texaspayments.com collector adapter (design §25) — ONE fetcher, agency-parameterised.

Covers the self-collecting Dallas County offices that bill outside ACT: Garland ISD, City of Garland,
Richardson ISD, Carrollton-Farmers Branch ISD — **4 of the 5 mapped collectors, 118 of 126 external
collector rows on the live book**. Adding another GDS office is a roster entry, not code.

WHY THESE CHOICES

  · **CAD NUMBER IS THE KEY, never an address.** Both systems share the 17-char parcel id and we
    already store it. That designs the entire address-normalisation problem out of existence — the
    LYNNACRE/"Lynna Cre" class of defect cannot reach this fetch. Address search stays unused.
  · **AGENCY IDS COME FROM THE ROSTER FILE**, resolved by name at call time. No literal ever appears
    here; `test_set_invariance` asserts it. Same discipline the DALLAS|PARKLAND regex broke.
  · **MEMBERSHIP BEFORE BALANCE.** `fetch_for_case` refuses to query a collector the petition did not
    name. Querying by geography is how you enrich against the wrong district.
  · **FAIL-SOFT, ALWAYS.** Any failure — timeout, no match, markup change, a parse that yields
    nothing — returns None for that collector, which the payoff schema renders `unavailable` →
    INDETERMINATE. **Never $0, and never blocks the ACT/DCAD/petition enrichment already landed.**

SCRAPING IS LOCAL. This module is not imported by the web app; it runs during local enrichment and
writes balances into `property_intel.collector_balances`, which the served engine merely reads.
"""
import asyncio
import re

import jurisdictions

BASE = "https://www.texaspayments.com"
NAV_TIMEOUT_MS = 40000


def parse_account_detail(text: str) -> dict:
    """One GDS account page → {account, cad, jurisdiction, years:[{year, levy, due}], amount_due}.

    `amount_due` is the SUM of the per-year Amount Due column — NOT the single year whose detail block
    happens to be expanded. On 3909 Cambridge the expanded block showed $4,086.97 while the account
    actually owed $12,108.43 across three years; reading the block alone would understate by 3x.

    Returns {} when the page is not an account detail (no match, an error page, changed markup) so the
    caller fails soft rather than inventing a number."""
    if not text:
        return {}
    t = text.replace("\xa0", " ")
    if "Account Number" not in t or "No Matches" in t:
        return {}

    def one(pat):
        # None, not "" — an absent field is unknown, not an empty value. (`jurisdiction` is genuinely
        # absent on a headless fetch: that block only renders for a year whose detail box is ticked.)
        m = re.search(pat, t)
        return (m.group(1).strip() or None) if m else None

    years, total = [], 0.0
    # rows render as: \t2025\t$3,428.98\t$4,896.59
    for y, levy, due in re.findall(r"\t(\d{4})\t\$([\d,]+\.\d{2})\t\$([\d,]+\.\d{2})", t):
        amt = float(due.replace(",", ""))
        years.append({"year": int(y), "levy": float(levy.replace(",", "")), "due": amt})
        total += amt
    if not years:
        return {}
    return {
        "account": one(r"Account Number:\s*\n?\s*(\S+)"),
        "cad": one(r"CAD Number:\s*\n?\s*(\S+)"),
        "jurisdiction": one(r"\d{4}\s*-\s*([A-Z][A-Z ]+)"),
        "owner": one(r"Owner Name:\s*\n?\s*(.+)"),
        "lawsuit": bool(re.search(r"Lawsuit\s*:\s*\n?\s*Yes", t)),
        "years": years,
        "amount_due": round(total, 2),
    }


async def fetch_one(page, agency: str, cad: str, attempts: int = 2) -> dict:
    """Query ONE agency for ONE parcel by CAD number. Returns {} on any failure — never raises.

    RETRIES ONCE. Measured on the first fleet run: 4 of 63 cases came back empty, and ALL FOUR hit on
    a straight retry — including 729 Woodcastle, which was already known by hand to be on the GISD
    roll, and a Richardson ISD parcel owing $14,082.76. Fail-soft is the right FINAL state, but
    accepting a transient failure as the final state silently discards recoverable debt."""
    for i in range(max(1, attempts)):
        got = await _fetch_once(page, agency, cad)      # PortalUnavailable propagates to the caller
        if got:
            return got
        if i + 1 < attempts:
            await asyncio.sleep(2)
    return {}


class PortalUnavailable(RuntimeError):
    """The PORTAL is refusing us — not an answer about a parcel.

    Discovered the hard way: after the ~200-request fleet run, texaspayments began 302-ing every
    request to /Error/WrongRequest. `parse_account_detail` saw an error page, found no account, and
    returned {} — which the caller then recorded as `unavailable`, i.e. INDISTINGUISHABLE FROM "this
    parcel is not on the roll". Safe (nothing wrong was written) but misleading: six cases including
    one KNOWN by hand to be on both Garland rolls looked like clean negatives.

    Absence must be distinguishable from a negative — the same principle as `unavailable` vs `$0`,
    one layer up. A throttled portal is an infrastructure condition to retry, not a fact about a
    parcel, and it must never be mistaken for one."""


BLOCK_URL_MARKER = "/Error/WrongRequest"
PROBE_AGENCY = "057909"          # any REAL agency page; the block only hits real portal pages


def portal_blocked(agency: str = PROBE_AGENCY, timeout: float = 15.0):
    """Is the portal refusing this host RIGHT NOW? -> (blocked: bool, detail: str). ~200ms, no browser.

    WHY THIS EXISTS. The block is an application-level 302 to /Error/WrongRequest, and the site states
    the reason itself: "You have been redirected to this page possibly due to your IP location."
    Detecting that needed a full chromium launch and a per-case search flow — ~10s to learn something
    a single GET answers instantly. That made polling for recovery expensive and testing a different
    network slow, so neither happened often enough.

    ⚠ THIS IS THE ONE SAFE USE OF A PLAIN GET HERE, AND ONLY BECAUSE IT CHECKS THE REDIRECT, NOT THE
    STATUS. §32.6's trap was reading `200 OK` on the landing page as "portal healthy" — the error page
    ITSELF returns 200. A cleared probe is necessary but NOT sufficient evidence the parcel-search
    flow works; confirm with the real adapter (`python3 collectors_gds.py <CAD>`) before a batch.

    Diagnostic worth keeping: a NONEXISTENT agency (e.g. /057124) returns a normal 404 even while
    blocked, so the host is not firewalled — the server still processes requests. The refusal is
    scoped to real portal pages, which is information the vendor can act on."""
    import httpx
    try:
        r = httpx.get(f"{BASE}/{agency}", timeout=timeout, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                                             "Chrome/124.0.0.0 Safari/537.36"})
    except Exception as e:
        return True, f"probe failed: {type(e).__name__}: {e}"
    if BLOCK_URL_MARKER in str(r.url):
        return True, f"redirected to {r.url} (site says: possibly due to your IP location)"
    if r.status_code == 200 and len(r.text) > 10000:
        return False, f"agency page served normally ({len(r.text)} bytes)"
    return True, f"unexpected response: HTTP {r.status_code}, {len(r.text)} bytes -> {r.url}"


async def _fetch_once(page, agency: str, cad: str) -> dict:
    try:
        await page.goto(f"{BASE}/{agency}", wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        await asyncio.sleep(1.5)
        # The portal is refusing us outright — say so, do not let it look like a parcel answer.
        if "/Error/" in page.url or not await page.query_selector("#searchValue"):
            raise PortalUnavailable(f"{agency}: portal returned {page.url}")
        # The search-method radios are hidden behind a kendo widget, so set the underlying input and
        # fire its change event — driving the widget's own chrome is brittle and unnecessary.
        await page.evaluate("""() => {
            const r = document.querySelector('#cad');
            if (!r) return;
            r.checked = true;
            r.dispatchEvent(new Event('change', {bubbles: true}));
            r.dispatchEvent(new Event('click',  {bubbles: true}));
        }""")
        await page.fill("#searchValue", cad)
        await page.click("button[type=submit]")
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(1.5)
        return parse_account_detail(await page.inner_text("body"))
    except PortalUnavailable:
        raise                      # infrastructure, not a parcel answer — must not be swallowed
    except Exception:
        return {}


async def fetch_for_case(collectors, cad: str, browser, roster=None) -> dict:
    """Balances for the collectors THE PETITION NAMED, keyed by canonical collector name.

    `collectors` is the petition's membership list. A collector that is not a GDS office, has no
    agency in the roster, or fails to fetch is simply ABSENT from the result — absence is what the
    payoff schema turns into `unavailable`, and that is the honest state."""
    out = {}
    if not cad or not collectors:
        return out
    roster = jurisdictions.load_gds_roster() if roster is None else roster
    context = await browser.new_context()
    try:
        page = await context.new_page()
        for name in collectors:
            info = jurisdictions.resolve_collector(name, roster=roster)
            if not info or info["platform"] != "gds" or not info["agency"]:
                continue
            try:
                got = await fetch_one(page, info["agency"], cad)
            except PortalUnavailable as e:
                # Record it as an INFRASTRUCTURE fault, distinct from a per-parcel negative, and stop
                # hammering a portal that is refusing us.
                out.setdefault("_portal_unavailable", []).append(str(e))
                break
            if not got:
                continue                      # fail-soft → stays `unavailable`
            # IDENTITY GUARD. Confirm the page we got back is for the parcel we asked about. A search
            # that silently resolves to a different account is exactly the cross-contamination class
            # that put one parcel's enrichment into another case's row (2.1% of the book). A result
            # we cannot tie to the requested CAD is discarded, not stored.
            if (got.get("cad") or "").strip() != cad.strip():
                out.setdefault("_rejected", []).append(
                    {"collector": info["collector"], "requested_cad": cad, "returned_cad": got.get("cad")})
                continue
            out[info["collector"]] = {
                "amount": got["amount_due"], "account": got["account"], "cad": got.get("cad"),
                "agency": info["agency"], "jurisdiction": got.get("jurisdiction"),
                "lawsuit": got.get("lawsuit"), "years": got["years"], "source": "gds",
            }
    finally:
        await context.close()
    return out


async def fetch_case_balances(tax_breakdown, cad, browser, roster=None) -> dict:
    """Convenience wrapper: petition breakdown → collector balances. MEMBERSHIP BEFORE BALANCE — the
    collector list comes from the suit, never from the address."""
    named = [c["collector"] for c in jurisdictions.petition_collectors(tax_breakdown)]
    return await fetch_for_case(named, cad, browser, roster=roster)


if __name__ == "__main__":  # manual probe: python3 collectors_gds.py <CAD>  |  --watch  |  --check
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] in ("--check", "--watch"):
        import time as _t
        _every = 300
        while True:
            _blocked, _why = portal_blocked()
            _stamp = _t.strftime("%H:%M:%S")
            print(f"[{_stamp}] {'BLOCKED ' if _blocked else 'REACHABLE'} — {_why}", flush=True)
            if _sys.argv[1] == "--check":
                _sys.exit(1 if _blocked else 0)
            if not _blocked:
                print("\n  Portal is answering again. CONFIRM with the real adapter before a batch —")
                print("  a served landing page does NOT prove the parcel-search flow works (§32.6):")
                print("      python3 collectors_gds.py 26238500070260000")
                _sys.exit(0)
            _t.sleep(_every)

    import json
    import sys
    from browser_env import chrome_path

    async def _main():
        from playwright.async_api import async_playwright
        cad = sys.argv[1]
        tb = [{"entity": "GARLAND INDEPENDENT SCHOOL DISTRICT"}, {"entity": "CITY OF GARLAND"}]
        async with async_playwright() as p:
            b = await p.chromium.launch(executable_path=chrome_path())
            print(json.dumps(await fetch_case_balances(tb, cad, b), indent=2))
            await b.close()

    asyncio.run(_main())
