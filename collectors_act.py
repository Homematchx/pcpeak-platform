#!/usr/bin/env python3
"""ACT-instance collector adapter (design §30) — for districts running their OWN Appraisal &
Collection Technologies portal rather than billing through Dallas County's.

Currently Irving ISD (`actweb.acttax.com/act_webdev/irving/`). The instance path is registry DATA on
the collector spec, not a literal in this fetcher, so a second ACT district is a registry entry.

WHY THIS IS THE SIMPLE ADAPTER. It is the SAME software as dallasact.com, so the detail page is a
plain `showdetail2.jsp?can=<CAD>&ownerno=0` GET — no session, no widget, no browser. That matters
beyond convenience: it runs without Playwright, so the backfill can query it in-process.

SAME CONTRACT AS THE GDS ADAPTER
  · CAD number is the key — no address normalisation anywhere.
  · IDENTITY GUARD — the page echoes the account it answered for; a response we cannot tie to the
    requested CAD is DISCARDED, not stored. This is the guard the gds adapter shipped without and
    nearly stored contaminated balances for.
  · RETRY ONCE — the gds fleet run had 4 of 63 cases fail transiently and ALL FOUR recovered on a
    retry. Fail-soft is the right final state; accepting a transient failure as final silently
    discards recoverable debt.
  · FAIL-SOFT — any failure returns {} and the collector line stays `unavailable` → INDETERMINATE.
    Never $0.
"""
import re
import ssl
import time
import urllib.request

import certifi

import jurisdictions

BASE = "https://actweb.acttax.com/act_webdev"
_CTX = ssl.create_default_context(cafile=certifi.where())


def _money(s):
    """'$2,537.62' → 2537.62 · absent → None. A real $0.00 stays 0.0 (verified nothing owed)."""
    if s is None:
        return None
    m = re.search(r"-?[\d,]+\.\d{2}", str(s))
    return float(m.group(0).replace(",", "")) if m else None


def parse_act_detail(text: str) -> dict:
    """One ACT detail page → {account, site_address, levy, amount_due, lawsuits}.

    Returns {} when the page is not a parcel detail (bad account, error page, changed markup) so the
    caller fails soft instead of inventing a number."""
    if not text or "Account Number" not in text:
        return {}
    t = re.sub(r"&nbsp;?", " ", re.sub(r"<[^>]+>", "\n", text))
    t = re.sub(r"[ \t]+", " ", t)

    def after(label, pat=r"\$?[\d,]+\.\d{2}"):
        m = re.search(re.escape(label) + r"\s*:?\s*\n?\s*(" + pat + r")", t)
        return m.group(1).strip() if m else None

    acct = after("Account Number", r"[0-9A-Za-z]{6,20}")
    total = _money(after("Total Amount Due"))
    if not acct or total is None:
        return {}
    site = after("Property Site Address", r"[^\n]+")
    law = after("Active Lawsuits", r"[^\n]+")
    return {"account": acct, "site_address": (site or "").strip() or None,
            "levy": _money(after("Current Tax Levy")), "amount_due": total,
            "lawsuits": None if law is None else (law.strip().lower() != "none")}


def fetch_one(cad: str, act_path: str, attempts: int = 2) -> dict:
    """Query ONE ACT instance for ONE parcel. Returns {} on any failure — never raises."""
    url = f"{BASE}/{act_path}/showdetail2.jsp?can={cad}&ownerno=0"
    for i in range(max(1, attempts)):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, context=_CTX, timeout=30) as f:
                got = parse_act_detail(f.read().decode("utf8", "ignore"))
            if got:
                return got
        except Exception:
            pass
        if i + 1 < attempts:
            time.sleep(2)
    return {}


def fetch_for_case(collectors, cad: str, roster=None) -> dict:
    """Balances for the collectors THE PETITION NAMED that live on an ACT instance.

    Membership before balance: a collector the suit did not name is never queried. A collector we
    cannot tie to the requested CAD is discarded — absence becomes `unavailable`, which is honest."""
    out = {}
    if not cad or not collectors:
        return out
    for name in collectors:
        info = jurisdictions.resolve_collector(name, roster=roster)
        if not info or info["platform"] != "irving_act":
            continue
        path = jurisdictions.act_path_for(info["collector"])
        if not path:
            continue
        got = fetch_one(cad, path)
        if not got:
            continue
        # IDENTITY GUARD — the page must answer for the parcel we asked about.
        if (got.get("account") or "").strip() != cad.strip():
            out.setdefault("_rejected", []).append(
                {"collector": info["collector"], "requested_cad": cad,
                 "returned_account": got.get("account")})
            continue
        out[info["collector"]] = {
            "amount": got["amount_due"], "account": got["account"], "cad": got["account"],
            "agency": path, "site_address": got.get("site_address"),
            "lawsuit": got.get("lawsuits"), "levy": got.get("levy"), "source": "irving_act",
        }
    return out


def fetch_case_balances(tax_breakdown, cad, roster=None) -> dict:
    """Petition breakdown → ACT-instance collector balances. Membership comes from the suit."""
    named = [c["collector"] for c in jurisdictions.petition_collectors(tax_breakdown)]
    return fetch_for_case(named, cad, roster=roster)


if __name__ == "__main__":  # manual probe: python3 collectors_act.py <CAD>
    import json
    import sys
    print(json.dumps(fetch_case_balances(
        [{"entity": "IRVING INDEPENDENT SCHOOL DISTRICT"}], sys.argv[1]), indent=2))
