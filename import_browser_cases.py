"""
Import browser-exported cases into the PC Peak SQLite database.
Run from pcpeak_platform folder:
  python3 import_browser_cases.py tax_foreclosure_2026-07-06.json
"""
import json
import sqlite3
import sys
from pathlib import Path
from datetime import datetime

DB_PATH = Path("data/db/pcpeak.db")
JSON_FILE = sys.argv[1] if len(sys.argv) > 1 else "tax_foreclosure_2026-07-06.json"

with open(JSON_FILE) as f:
    cases = json.load(f)

# Deduplicate by case number — keep richest record
seen = {}
for c in cases:
    ex = c.get("extracted", {})
    cn = ex.get("caseNumber", "")
    if not cn:
        continue
    # Prefer record with more data
    if cn not in seen or (ex.get("totalDueAtFiling", 0) or 0) > (seen[cn].get("totalDueAtFiling", 0) or 0):
        seen[cn] = ex
        seen[cn]["_memo"] = c.get("memo", "") or ex.get("aiNotes", "")
        seen[cn]["_city"] = c.get("city", "dallas")

print(f"Unique cases to import: {len(seen)}")

with sqlite3.connect(str(DB_PATH)) as db:
    # Get existing case numbers
    existing = {r[0] for r in db.execute("SELECT case_number FROM cases").fetchall()}
    print(f"Existing in DB: {len(existing)}")

    imported = 0
    updated = 0

    for cn, ex in seen.items():
        total_due = ex.get("totalDueAtFiling") or 0
        if isinstance(total_due, str):
            try:
                total_due = float(total_due.replace(",", "").replace("$", ""))
            except Exception:
                total_due = 0

        data = {
            "case_number":      cn,
            "court":            ex.get("court", ""),
            "judicial_officer": ex.get("judicialOfficer", ""),
            "filed_date":       ex.get("filedDate", ""),
            "case_status":      ex.get("caseStatus", "OPEN"),
            "case_type":        "TAX DELINQUENCY",
            "defendant":        ex.get("defendant", ""),
            "all_defendants":   json.dumps(ex.get("allDefendants", [])),
            "property_address": ex.get("propertyAddress", ""),
            "account_number":   ex.get("accountNumber", ""),
            "law_firm":         ex.get("lawFirm", "LGBS (Linebarger)"),
            "plaintiff_attorney": ex.get("plaintiffAttorney", ""),
            "total_due_filing": total_due if total_due else None,
            "oldest_delinquency_year": ex.get("oldestDelinquencyYear") or None,
            "delinquency_years": json.dumps(ex.get("delinquencyYears", [])),
            "def_count":        ex.get("defCount", 1),
            "cbp_requested":    1 if ex.get("citationByPostingRequested") else 0,
            "rule106":          1 if ex.get("rule106SubstituteService") else 0,
            "prior_suits":      json.dumps(ex.get("priorRelatedSuits", [])),
            "estate_heir":      1 if ex.get("estateHeirSituation") else 0,
            "continuance_count": ex.get("continuanceCount", 0),
            "trial_reset_count": ex.get("trialResetCount", 0),
            "service_issues":   ex.get("serviceIssues", ""),
            "complexity":       ex.get("complexity", "low"),
            "complexity_reason": ex.get("complexityReason", ""),
            "judgment_date":    ex.get("judgmentDate") or None,
            "judgment_type":    ex.get("judgmentType", "none"),
            "next_hearing_date": ex.get("nextHearingDate") or None,
            "tax_breakdown":    json.dumps(ex.get("taxBreakdown", [])),
            "ai_memo":          ex.get("_memo", ""),
            "city":             ex.get("_city", "dallas"),
            "monitored":        1,
            "owner_type":       "individual",
            "owner_priority":   "high",
            "created_at":       datetime.now().isoformat(),
            "updated_at":       datetime.now().isoformat(),
        }

        if cn in existing:
            # Update only if browser version has richer data
            db.execute("""
                UPDATE cases SET
                    court = COALESCE(NULLIF(:court,''), court),
                    filed_date = COALESCE(NULLIF(:filed_date,''), filed_date),
                    defendant = COALESCE(NULLIF(:defendant,''), defendant),
                    property_address = COALESCE(NULLIF(:property_address,''), property_address),
                    total_due_filing = COALESCE(:total_due_filing, total_due_filing),
                    tax_breakdown = COALESCE(NULLIF(:tax_breakdown,'[]'), tax_breakdown),
                    ai_memo = COALESCE(NULLIF(:ai_memo,''), ai_memo),
                    updated_at = :updated_at
                WHERE case_number = :case_number
            """, data)
            updated += 1
            print(f"  UPDATED: {cn} | {ex.get('defendant')} | ${total_due:,.2f}")
        else:
            # Insert new case
            cols = ", ".join(data.keys())
            vals = ", ".join(f":{k}" for k in data.keys())
            db.execute(f"INSERT OR IGNORE INTO cases ({cols}) VALUES ({vals})", data)
            imported += 1
            print(f"  IMPORTED: {cn} | {ex.get('defendant')} | ${total_due:,.2f}")

    db.commit()

total = db.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
print(f"\nDone — Imported: {imported} | Updated: {updated} | Total in DB: {total}")
