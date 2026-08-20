#!/usr/bin/env python3
"""§34 backfill — populate `property_intel.act_units` from ACT's per-parcel jurisdiction report.

WHY A TARGETED BACKFILL AND NOT A FULL RE-ENRICHMENT. `enrich_property()` rewrites the whole
`property_intel` blob (DCAD + ACT + ownership + payments + distress). Re-running it fleet-wide to
acquire ONE new field would put every other field's current value at risk of a re-scrape regression,
for no benefit. This follows the `intel_backfill.py` pattern instead: fetch exactly the one report,
merge exactly the two keys, leave every other byte of the blob alone.

NO PLAYWRIGHT. `taxbyyearbyunit.jsp` is server-rendered — verified — so a plain GET returns the full
table. That makes this cheap and re-runnable.

THREE OUTCOMES, STORED DISTINGUISHABLY (§34.2). The $0 case is the one that matters:

    act_unit_list                 units captured; completeness can now be PROVEN
    no_unit_list_at_zero_balance  ACT prints "No taxes due." and NO unit list — NEVER retryable
    fetch_failed / unrecognized   transport or shape problem — retryable

A blank list must never be stored as `[]`. `[]` would read as "ACT bills nothing on this parcel",
which is absence-as-a-value and the exact false-complete §33 closed. This writes None + the reason.

MULTI-TRACT IS ALL-OR-NOTHING, matching `_aggregate_multi_tract`: coverage is the union only when
EVERY tract reported a list; if any tract is at $0 the parcel's coverage is UNKNOWN, because a
district could bill that tract and nothing would reveal it.

  (default)   dry run — fetch and report, write nothing
  --write     merge into property_intel
  --case CN   restrict to one case
"""
import argparse
import concurrent.futures as cf
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import httpx
from property_intel import parse_act_units

ROOT = Path(__file__).parent
DB = ROOT / "data" / "db" / "pcpeak.db"
BASE_WHERE = "property_type IS NOT 'personal' AND case_track IS NOT 'personal_property'"
URL = ("https://www.dallasact.com/act_webdev/dallas/reports/taxbyyearbyunit.jsp"
       "?can={acct}&ownerno=0")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
ACCT_RE = re.compile(r"^[0-9A-Z]{17}$")


def html_to_text(html: str) -> str:
    """The same reduction `parse_act_units` was validated against."""
    html = re.sub(r"<script.*?</script>", "", html, flags=re.S | re.I)
    html = re.sub(r"<style.*?</style>", "", html, flags=re.S | re.I)
    return re.sub(r"&nbsp;?", " ", re.sub(r"<[^>]+>", "\n", html))


def accounts_of(raw) -> list:
    return [a for a in (x.strip().upper() for x in str(raw or "").split(",")) if ACCT_RE.match(a)]


def fetch_units(acct: str, client) -> tuple:
    try:
        r = client.get(URL.format(acct=acct), timeout=40, headers=UA, follow_redirects=True)
        if r.status_code != 200:
            return None, "fetch_failed"
        return parse_act_units(html_to_text(r.text))
    except Exception:
        return None, "fetch_failed"


def resolve_case(accts, client):
    """Per-parcel coverage across every tract — all-or-nothing (§34, _aggregate_multi_tract)."""
    per = [fetch_units(a, client) for a in accts]
    if all(u for u, _ in per):
        merged = []
        for u, _ in per:
            for name in u:
                if name not in merged:
                    merged.append(name)
        return merged, "act_unit_list"
    return None, next((why for u, why in per if not u), "unrecognized_page")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--case")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    q = f"SELECT case_number, property_intel FROM cases WHERE {BASE_WHERE}"
    params = ()
    if args.case:
        q += " AND case_number=?"
        params = (args.case,)
    rows = con.execute(q, params).fetchall()

    work = []
    no_acct = 0
    for r in rows:
        try:
            pi = json.loads(r["property_intel"] or "{}")
        except ValueError:
            pi = {}
        accts = accounts_of(pi.get("account_number"))
        if not accts:
            no_acct += 1
            continue
        work.append((r["case_number"], accts, pi.get("current_tax_balance")))

    print(f"cases: {len(rows)}   no usable account: {no_acct}   to fetch: {len(work)}\n")
    client = httpx.Client()
    out, t0 = {}, time.time()

    def one(item):
        cn, accts, bal = item
        u, why = resolve_case(accts, client)
        return cn, u, why, bal

    done = 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for cn, u, why, bal in ex.map(one, work):
            out[cn] = (u, why, bal)
            done += 1
            if done % 50 == 0:
                print(f"  … {done}/{len(work)}  ({time.time()-t0:.0f}s)", flush=True)

    import collections
    census = collections.Counter(why for _, why, _ in out.values())
    print(f"\nfetched {len(out)} in {time.time()-t0:.0f}s\n")
    print("OUTCOME CENSUS:")
    for why, n in census.most_common():
        print(f"   {n:4d}  {why}")

    # The §34.2 cross-check: the $0 reason must land on $0 parcels and nowhere else.
    mism = [(cn, why, bal) for cn, (u, why, bal) in out.items()
            if why == "no_unit_list_at_zero_balance" and isinstance(bal, (int, float)) and bal > 0]
    print(f"\n'no_unit_list_at_zero_balance' on a parcel with a NONZERO stored balance: {len(mism)}"
          + ("  <-- investigate" if mism else "  (consistent)"))
    for m in mism[:6]:
        print("   ", m)
    got = [(cn, u) for cn, (u, why, _) in out.items() if why == "act_unit_list"]
    print(f"\nparcels with a real unit list: {len(got)}")
    if got:
        sizes = collections.Counter(len(u) for _, u in got)
        print("   units-per-parcel:", dict(sorted(sizes.items())))
        names = collections.Counter(n for _, u in got for n in u)
        print("   most common units:", names.most_common(8))

    if not args.write:
        print("\nDRY RUN — nothing written. Re-run with --write to merge.")
        return

    n = 0
    for cn, (u, why, _) in out.items():
        row = con.execute("SELECT property_intel FROM cases WHERE case_number=?", (cn,)).fetchone()
        try:
            pi = json.loads(row["property_intel"] or "{}")
        except ValueError:
            continue
        # MERGE, never replace: only these two keys are touched.
        pi["act_units"] = u          # None stays None — never [] (§34.2)
        pi["act_units_reason"] = why
        con.execute("UPDATE cases SET property_intel=? WHERE case_number=?", (json.dumps(pi), cn))
        n += 1
    con.commit()
    print(f"\nWROTE act_units on {n} case(s) — two keys merged, rest of the blob untouched.")


if __name__ == "__main__":
    main()
