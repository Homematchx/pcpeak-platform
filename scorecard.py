#!/usr/bin/env python3
"""
Projection-vs-actual scorecard.

The timing prediction (projected Order of Sale) is the product's moat — but it's
only a moat if it's provably accurate. This backtests compute_projection() against
cases that have a REAL outcome (an actual oos_date), and reports:

  1. Prediction error — predicted OOS vs actual OOS (post-judgment and at-filing).
  2. Observed vs assumed windows — the actual filing->judgment and judgment->OOS
     timelines from closed cases vs what CITY_DATA assumes (i.e. how to recalibrate).
  3. Data-quality flags — e.g. oos_date == judgment_date (implausible 0-day window),
     which means the outcome field is being mis-extracted and can't be scored.

    python3 scorecard.py            # score the local DB
    SCORE_DB=/path/to.db python3 scorecard.py

Feed it more closed cases (discover.py --include-closed ...) and the numbers get real.
"""
import os, sqlite3, statistics
from datetime import datetime
from pathlib import Path
import importlib.util

DB = Path(os.environ.get("SCORE_DB", Path(__file__).parent / "data" / "db" / "pcpeak.db"))

# Import compute_projection + CITY_DATA from the backend without starting the server.
spec = importlib.util.spec_from_file_location("bmain", str(Path(__file__).parent / "backend" / "main.py"))
bmain = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(bmain)
except SystemExit:
    pass
compute_projection = bmain.compute_projection
CITY_DATA = bmain.CITY_DATA


def d(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d")
    except Exception:
        return None


def days(a, b):
    return (a - b).days if (a and b) else None


def main():
    db = sqlite3.connect(DB); db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute("SELECT * FROM cases").fetchall()]
    total = len(rows)

    # Ground truth = an actual Order-of-Sale date.
    resolved = [c for c in rows if d(c.get("oos_date"))]

    print("=" * 68)
    print("  PROJECTION-vs-ACTUAL SCORECARD")
    print("=" * 68)
    print(f"  cases: {total}   with an actual OOS date (scoreable): {len(resolved)}")
    if not resolved:
        print("\n  No cases with a real oos_date yet — backfill closed cases:")
        print("    python3 discover.py --pattern TX-25 --include-closed --individuals-only")
        return

    # --- Data-quality gate: OOS must plausibly follow judgment ---
    bad = []
    obs_joos, obs_ftj, obs_ftoos = [], [], []
    for c in resolved:
        fd, jd, od = d(c.get("filed_date")), d(c.get("judgment_date")), d(c.get("oos_date"))
        joos = days(od, jd)
        if jd and (joos is None or joos <= 0):
            bad.append((c["case_number"], c.get("judgment_date"), c.get("oos_date")))
        else:
            if joos is not None: obs_joos.append(joos)
            if days(jd, fd): obs_ftj.append(days(jd, fd))
            if days(od, fd): obs_ftoos.append(days(od, fd))

    if bad:
        print(f"\n  DATA-QUALITY FLAG: {len(bad)} case(s) have oos_date <= judgment_date")
        print("  (0-day post-judgment window is implausible — oos_date is likely being")
        print("   mis-extracted as the judgment date; these can't be scored):")
        for cn, jd, od in bad[:8]:
            print(f"    {cn}: judgment={jd}  oos={od}")

    # --- Observed vs assumed windows (Dallas) ---
    dj = CITY_DATA["dallas"]
    print("\n  OBSERVED vs MODEL windows (from scoreable closed cases):")
    if obs_ftj:
        print(f"    filing->judgment: observed median {round(statistics.median(obs_ftj)/30,1)} mo "
              f"(n={len(obs_ftj)})   model assumes {dj['ftj']}")
    if obs_joos:
        print(f"    judgment->OOS   : observed median {round(statistics.median(obs_joos))} d "
              f"(n={len(obs_joos)})   model assumes {dj['joos']}")

    # --- Prediction error: predicted OOS vs actual OOS ---
    def score(mode):
        errs = []
        for c in resolved:
            od = d(c.get("oos_date"))
            joos = days(od, d(c.get("judgment_date")))
            if joos is not None and joos <= 0:      # skip data-quality-flagged
                continue
            inp = dict(c)
            if mode == "at_filing":
                inp["judgment_date"] = ""            # predict as if only the filing were known
                inp["next_hearing_date"] = ""
            proj = compute_projection(inp).get("projected_oos")
            pj = d(proj)
            if pj and od:
                errs.append((pj - od).days)
        return errs

    for mode, label in [("post_judgment", "POST-JUDGMENT (judgment known)"),
                        ("at_filing", "AT-FILING (only filing known)")]:
        e = score(mode)
        print(f"\n  {label} — predicted OOS vs actual, n={len(e)}")
        if not e:
            print("    (none scoreable in this mode)")
            continue
        ae = [abs(x) for x in e]
        within = lambda k: round(100 * sum(1 for x in ae if x <= k) / len(ae))
        print(f"    mean abs error : {round(statistics.mean(ae))} days")
        print(f"    median abs err : {round(statistics.median(ae))} days")
        print(f"    within 30/60/90d: {within(30)}% / {within(60)}% / {within(90)}%")
        bias = round(statistics.mean(e))
        print(f"    bias           : {'+' if bias>=0 else ''}{bias} days "
              f"({'predicts LATE' if bias>0 else 'predicts EARLY' if bias<0 else 'unbiased'})")

    print("\n" + "=" * 68)


if __name__ == "__main__":
    main()
