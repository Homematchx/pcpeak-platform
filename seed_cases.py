"""
PC Peak — Seed Script
Pre-populates the cases table with all 5 benchmark cases
so the dashboard shows data before the agent runs.

Run: python3 seed_cases.py
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent / "data" / "db" / "pcpeak.db"

if not DB_PATH.exists():
    print(f"ERROR: Database not found at {DB_PATH}")
    print("Make sure the backend has been started at least once first:")
    print("  python3 backend/main.py")
    exit(1)

cases = [
    {
        "case_number": "TX-23-00042",
        "court": "160th District Court",
        "judicial_officer": "GINSBERG, CARL",
        "filed_date": "2023-01-13",
        "case_status": "OPEN",
        "defendant": "Williams, Ruby J. / Motley",
        "all_defendants": json.dumps(["Williams, Ruby J.", "Motley (heir)", "Unknown heirs"]),
        "property_address": "3928 Atlanta St, Dallas TX 75215",
        "account_number": "00000172327000000",
        "law_firm": "LGBS (Linebarger)",
        "plaintiff_attorney": "ATKINS, ASHLY STEELE",
        "total_due_filing": 25889.70,
        "oldest_delinquency_year": 2002,
        "delinquency_years": json.dumps(list(range(2002, 2023))),
        "def_count": 8,
        "cbp_requested": 1,
        "rule106": 1,
        "prior_suits": json.dumps(["TX-00-XXXXX", "TX-10-XXXXX"]),
        "estate_heir": 1,
        "continuance_count": 3,
        "trial_reset_count": 4,
        "service_issues": "Multiple CBP defendants, 5 service attempts on Willie A. Williams",
        "complexity": "high",
        "complexity_reason": "8 defendants, estate/heir situation, CBP required, 3 continuances, 2 prior suits",
        "judgment_date": "2026-02-16",
        "judgment_type": "nonjury",
        "oos_date": "2026-05-15",
        "oos_issued": 1,
        "next_hearing_date": None,
        "city": "dallas",
        "stage": "oos_issued",
        "similar_benchmark": "Self — confirmed benchmark",
        "ai_memo": "CONFIRMED BENCHMARK — TX-23-00042 is a fully resolved high-complexity Dallas case. Filed January 13, 2023, judgment entered February 16, 2026 (37 months), Order of Sale issued May 15, 2026 (89 days post-judgment). This is the definitive calibration point for high-complexity Dallas cases with estate/heir situations and CBP defendants. All projections for similar cases should use 30-40 month filing-to-judgment and 85-90 day judgment-to-OOS windows.",
        "tax_breakdown": json.dumps([
            {"entity": "Dallas ISD", "taxAmt": 18000, "penaltyInterest": 5000, "total": 23000},
            {"entity": "City of Dallas", "taxAmt": 1500, "penaltyInterest": 400, "total": 1900},
            {"entity": "Dallas County", "taxAmt": 600, "penaltyInterest": 200, "total": 800},
        ]),
        "monitored": 1,
    },
    {
        "case_number": "TX-25-00492",
        "court": "193rd District Court",
        "judicial_officer": "GINSBERG, CARL",
        "filed_date": "2025-03-14",
        "case_status": "OPEN",
        "defendant": "Hedge",
        "all_defendants": json.dumps(["Hedge"]),
        "property_address": "2827 E. Overton Rd, Dallas TX 75216",
        "account_number": "00000509671000000",
        "law_firm": "LGBS (Linebarger)",
        "plaintiff_attorney": "ATKINS, ASHLY STEELE",
        "total_due_filing": 13704.32,
        "oldest_delinquency_year": 2021,
        "delinquency_years": json.dumps([2021, 2022, 2023, 2024]),
        "def_count": 1,
        "cbp_requested": 0,
        "rule106": 1,
        "prior_suits": json.dumps([]),
        "estate_heir": 0,
        "continuance_count": 1,
        "trial_reset_count": 0,
        "service_issues": "Initial citation failed — Rule 106 substitute service required, completed 12/29/2025",
        "complexity": "low",
        "complexity_reason": "1 defendant, Rule 106 resolved, 1 continuance, default judgment, no prior suits",
        "judgment_date": "2026-05-18",
        "judgment_type": "default",
        "oos_date": None,
        "oos_issued": 0,
        "next_hearing_date": None,
        "city": "dallas",
        "stage": "judgment_entered",
        "similar_benchmark": "TX-23-00042 (low complexity baseline)",
        "ai_memo": "Judgment entered May 18, 2026 via default — 14 months from filing. This is the primary low-complexity Dallas benchmark. Single defendant, Rule 106 service completed December 29, 2025 (290 days to serve), 1 continuance, default judgment. OOS projected approximately August 2026 using 60-89 day post-judgment window. Appeal period closed approximately June 18, 2026. Acquisition opportunity: clean title, single defendant, no estate complications. Monitor for OOS issuance.",
        "tax_breakdown": json.dumps([
            {"entity": "Dallas ISD", "taxAmt": 8000, "penaltyInterest": 2500, "total": 10500},
            {"entity": "City of Dallas", "taxAmt": 1800, "penaltyInterest": 500, "total": 2300},
            {"entity": "Dallas County", "taxAmt": 700, "penaltyInterest": 200, "total": 900},
        ]),
        "monitored": 1,
    },
    {
        "case_number": "TX-25-01777",
        "court": "134th District Court",
        "judicial_officer": "GINSBERG, CARL",
        "filed_date": "2025-10-28",
        "case_status": "OPEN",
        "defendant": "Stewart, Benny J.",
        "all_defendants": json.dumps(["Stewart, Benny J."]),
        "property_address": "Dallas TX (address TBD)",
        "account_number": "TBD",
        "law_firm": "LGBS (Linebarger)",
        "plaintiff_attorney": "ZOKAIE, CHEYENNE MATHEW",
        "total_due_filing": None,
        "oldest_delinquency_year": None,
        "delinquency_years": json.dumps([]),
        "def_count": 1,
        "cbp_requested": 0,
        "rule106": 0,
        "prior_suits": json.dumps([]),
        "estate_heir": 0,
        "continuance_count": 0,
        "trial_reset_count": 0,
        "service_issues": "",
        "complexity": "low",
        "complexity_reason": "1 defendant, served in 43 days, no complications, no prior suits",
        "judgment_date": None,
        "judgment_type": "none",
        "oos_date": None,
        "oos_issued": 0,
        "next_hearing_date": "2026-07-20",
        "city": "dallas",
        "stage": "pre_judgment",
        "similar_benchmark": "TX-25-00492 (Hedge) — same low complexity profile",
        "ai_memo": "PENDING — Trial scheduled July 20, 2026. Cleanest case in the benchmark set: 1 defendant, served in only 43 days, no continuances, no CBP, no ad litem, no prior suits. Using Hedge benchmark (14 months, low complexity): judgment expected late July 2026 if no continuances. OOS projected September-October 2026. This is the fastest-moving active case in the portfolio. Watch for any answer filed or continuance request before July 20.",
        "tax_breakdown": json.dumps([]),
        "monitored": 1,
    },
    {
        "case_number": "TX-23-00569",
        "court": "162nd District Court",
        "judicial_officer": "GINSBERG, CARL",
        "filed_date": "2023-04-03",
        "case_status": "OPEN",
        "defendant": "Williams, Paula E. / Chester F. Williams Est.",
        "all_defendants": json.dumps(["Williams, Paula E.", "Williams, Ronald A. Sr.", "Williams, Ronald A. Jr.", "Williams, Gloria V.", "Williams, Chester F. (Estate)"]),
        "property_address": "1506 Harbor Road, Dallas TX 75216",
        "account_number": "00000503698000000",
        "law_firm": "LGBS (Linebarger)",
        "plaintiff_attorney": "ATKINS, ASHLY STEELE",
        "total_due_filing": 112449.25,
        "oldest_delinquency_year": 1991,
        "delinquency_years": json.dumps(list(range(1991, 2023))),
        "def_count": 5,
        "cbp_requested": 1,
        "rule106": 1,
        "prior_suits": json.dumps(["TX-00-31060", "TX-18-01607"]),
        "estate_heir": 1,
        "continuance_count": 0,
        "trial_reset_count": 0,
        "service_issues": "Paula Williams served Rule 106. Ronald Sr., Gloria, Chester (estate) cited by posting. Mark W. Sutherland as ad litem and defense attorney.",
        "complexity": "high",
        "complexity_reason": "5 defendants, deceased owner (estate), 35 years delinquency, 3 prior suits spanning 23 years, combined case 00-31060-T-C",
        "judgment_date": "2026-01-07",
        "judgment_type": "nonjury",
        "oos_date": "2026-04-20",
        "oos_issued": 1,
        "next_hearing_date": None,
        "city": "dallas",
        "stage": "sale_pulled",
        "assessed_value": 286730.00,
        "minimum_bid": 226910.25,
        "sale_scheduled_date": "2026-06-02",
        "sale_pulled_date": "2026-05-12",
        "similar_benchmark": "TX-23-00042 (similar high complexity, estate)",
        "ai_memo": "CRITICAL WATCH — Sale was scheduled for June 2, 2026 and PULLED on May 12, 2026 by LGBS attorney Tina M. via fax to Dallas County Sheriff. This is after 35 years of delinquency and 3 prior suits spanning 23 years. The most likely cause is a last-minute bankruptcy filing (automatic stay), payment agreement negotiated by ad litem Mark W. Sutherland, or a procedural challenge. Total debt at OOS: $216,554 against assessed value of $286,730 — debt is 75% of value leaving thin buyer equity at minimum bid of $226,910. Paula Williams appears to be living at the subject property (1506 Harbor Rd listed as her address on the OOS notice). This property WILL resurface — all procedural groundwork is complete. Monitor the docket weekly. When it reappears, the path to auction will be far shorter than the original 33 months.",
        "tax_breakdown": json.dumps([
            {"entity": "Dallas ISD", "taxAmt": 34640, "penaltyInterest": 75610, "total": 110251},
            {"entity": "City of Dallas", "taxAmt": 19921, "penaltyInterest": 40058, "total": 59979},
            {"entity": "Parkland Hospital", "taxAmt": 6668, "penaltyInterest": 13653, "total": 20321},
            {"entity": "Dallas County", "taxAmt": 5982, "penaltyInterest": 11991, "total": 17973},
            {"entity": "Dallas College", "taxAmt": 2681, "penaltyInterest": 4810, "total": 7491},
            {"entity": "School Equalization", "taxAmt": 174, "penaltyInterest": 365, "total": 539},
        ]),
        "monitored": 1,
    },
    {
        "case_number": "TX-26-00009",
        "court": "192nd District Court",
        "judicial_officer": "GINSBERG, CARL",
        "filed_date": "2026-01-05",
        "case_status": "OPEN",
        "defendant": "Rogers, Lucy Marilyn",
        "all_defendants": json.dumps(["Rogers, Lucy Marilyn"]),
        "property_address": "1218 Hudspeth Ave, Dallas TX 75216",
        "account_number": "00000303256000000",
        "law_firm": "LGBS (Linebarger)",
        "plaintiff_attorney": "ZOKAIE, CHEYENNE MATHEW",
        "total_due_filing": 19366.44,
        "oldest_delinquency_year": 2018,
        "delinquency_years": json.dumps([2018, 2019, 2021, 2022, 2023, 2024]),
        "def_count": 1,
        "cbp_requested": 0,
        "rule106": 0,
        "prior_suits": json.dumps([]),
        "estate_heir": 0,
        "continuance_count": 0,
        "trial_reset_count": 0,
        "service_issues": "Initial citation returned unexecuted 02/11/2026. Re-service required.",
        "complexity": "low",
        "complexity_reason": "1 defendant, known address, no prior suits, no estate — unexecuted citation is minor delay",
        "judgment_date": None,
        "judgment_type": "none",
        "oos_date": None,
        "oos_issued": 0,
        "next_hearing_date": "2026-09-30",
        "city": "dallas",
        "stage": "pre_judgment",
        "similar_benchmark": "TX-25-00492 (Hedge) — same low complexity, single defendant",
        "ai_memo": "PENDING — Trial scheduled September 30, 2026. Filed January 5, 2026. Single defendant Lucy Marilyn Rogers at a known address (5343 Marvin D Love Fwy Apt 151, Dallas). Low complexity despite the unexecuted citation returned February 11, 2026 — service will need to be re-attempted but Rogers has a confirmed address so this is a minor delay. Total due: $19,366 covering delinquent years 2018-2024. Dallas ISD is the primary creditor at $8,924. No prior suits, no estate complications. Using Hedge benchmark: judgment projected March 2027 if trial proceeds September 30, OOS projected May-June 2027. Key watch date: confirm re-service before September 30 trial.",
        "tax_breakdown": json.dumps([
            {"entity": "Dallas ISD", "taxAmt": 5153, "penaltyInterest": 3771, "total": 8924},
            {"entity": "City of Dallas", "taxAmt": 3426, "penaltyInterest": 2464, "total": 5890},
            {"entity": "Parkland Hospital", "taxAmt": 1070, "penaltyInterest": 779, "total": 1849},
            {"entity": "Dallas County", "taxAmt": 1020, "penaltyInterest": 733, "total": 1753},
            {"entity": "Dallas College", "taxAmt": 526, "penaltyInterest": 381, "total": 907},
            {"entity": "School Equalization", "taxAmt": 22, "penaltyInterest": 20, "total": 42},
        ]),
        "monitored": 1,
    },
]

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

seeded = 0
updated = 0
now = datetime.now().isoformat()

for case in cases:
    case["created_at"] = now
    case["updated_at"] = now
    case["last_agent_run"] = None

    existing = conn.execute("SELECT id FROM cases WHERE case_number=?", [case["case_number"]]).fetchone()
    if existing:
        sets = ", ".join([f"{k}=?" for k in case if k != "case_number"])
        vals = [case[k] for k in case if k != "case_number"] + [case["case_number"]]
        conn.execute(f"UPDATE cases SET {sets} WHERE case_number=?", vals)
        updated += 1
    else:
        cols = ", ".join(case.keys())
        ph = ", ".join(["?" for _ in case])
        conn.execute(f"INSERT INTO cases ({cols}) VALUES ({ph})", list(case.values()))
        seeded += 1

# Seed docket events for each case
events = [
    ("TX-23-00042", "2023-01-13", "filing", "New case filed (OCA) — Civil", "Tax delinquency suit", 0),
    ("TX-23-00042", "2023-01-13", "filing", "Original Petition filed", "Real property — tax delinquency 2002-2022", 0),
    ("TX-23-00042", "2026-02-16", "judgment", "Non-jury judgment entered", "Judge Ginsberg — 162nd District Court", 0),
    ("TX-23-00042", "2026-05-15", "oos", "Order of Sale issued", "CONFIRMED — 89 days post-judgment", 0),

    ("TX-25-00492", "2025-03-14", "filing", "New case filed (OCA) — Civil", "Tax delinquency suit", 0),
    ("TX-25-00492", "2025-12-29", "service", "Citation served — Rule 106", "Substitute service completed", 0),
    ("TX-25-00492", "2026-05-18", "judgment", "Default judgment entered", "Defendant failed to appear", 0),

    ("TX-25-01777", "2025-10-28", "filing", "New case filed (OCA) — Civil", "Tax delinquency suit", 0),
    ("TX-25-01777", "2025-12-11", "service", "Citation served", "Served in 43 days", 0),
    ("TX-25-01777", "2026-07-20", "hearing", "Non-jury trial scheduled", "Judge Ginsberg — 10:05 AM", 0),

    ("TX-23-00569", "2023-04-03", "filing", "New case filed (OCA) — Civil", "Tax delinquency suit — combined w/ TX-00-31060", 0),
    ("TX-23-00569", "2026-01-07", "judgment", "Non-jury judgment entered", "Judge Ginsberg — $148,369.52 total judgment", 0),
    ("TX-23-00569", "2026-04-20", "oos", "Order of Sale issued", "Sale scheduled June 2, 2026", 0),
    ("TX-23-00569", "2026-05-08", "motion", "Publication — Daily Commercial Record", "First of three required publications", 0),
    ("TX-23-00569", "2026-05-12", "motion", "Property PULLED from sheriff sale", "By fax from LGBS Tina M. — 20 days before auction", 1),

    ("TX-26-00009", "2026-01-05", "filing", "New case filed (OCA) — Civil", "Tax delinquency suit", 0),
    ("TX-26-00009", "2026-01-05", "filing", "Original Petition filed", "Real property — $19,366.44 total due", 0),
    ("TX-26-00009", "2026-01-08", "service", "Citation issued — Lucy Marilyn Rogers", "Private process server", 0),
    ("TX-26-00009", "2026-02-11", "service", "Return of service — UNEXECUTED", "Citation unexecuted — re-service required", 1),
    ("TX-26-00009", "2026-09-30", "hearing", "Non-jury trial scheduled", "Judge Ginsberg — 10:05 AM", 0),
]

ev_count = 0
for ev in events:
    existing_ev = conn.execute(
        "SELECT id FROM docket_events WHERE case_number=? AND event_date=? AND description=?",
        [ev[0], ev[1], ev[3]]
    ).fetchone()
    if not existing_ev:
        conn.execute(
            "INSERT INTO docket_events (case_number,event_date,event_type,description,detail,is_new) VALUES (?,?,?,?,?,?)",
            ev
        )
        ev_count += 1

conn.commit()
conn.close()

total = conn.execute if False else None
print(f"\n{'='*50}")
print(f"  PC Peak Database Seeded Successfully")
print(f"{'='*50}")
print(f"  Cases inserted:  {seeded}")
print(f"  Cases updated:   {updated}")
print(f"  Events added:    {ev_count}")
print(f"  Database:        {DB_PATH}")
print(f"{'='*50}")
print(f"\nNow refresh your dashboard at http://localhost:8080")
print(f"All 5 cases will appear with full data and AI memos.")
