import csv, sqlite3, re
from pathlib import Path

DB = "/Users/stephenlewis/Downloads/pcpeak_platform/data/db/pcpeak.db"
CSV = "/Users/stephenlewis/Downloads/Backtax application test file.csv"

BUSINESS_WORDS = ["LLC","INC","CORP","LTD","TRUST","PROPERTIES","HOLDINGS",
"INVESTMENTS","GROUP","REALTY","ASSOCIATES","MANAGEMENT","DEVELOPMENT",
"SERVICES","COMPANY","DBA","FUND","CITY","COMMUNITY","CHURCH","MINISTRY",
"FOUNDATION","PROGRAM","FUNERAL"]
ESTATE_WORDS = ["EST OF","ESTATE OF","ESTATE","HEIR","LIFE ESTATE"]

def classify(name):
    n = name.upper()
    if any(w in n for w in ESTATE_WORDS): return "estate","high"
    if any(w in n for w in BUSINESS_WORDS): return "business","medium"
    return "individual","high"

def to_courts(name):
    n = name.upper()
    for w in ESTATE_WORDS+["JR","SR","II","III","&","ET AL"]:
        n = n.replace(w,"")
    parts = n.split()
    if len(parts)>=2: return f"{parts[0]}, {parts[1]}"
    return parts[0] if parts else name

db = sqlite3.connect(DB)
db.execute("""CREATE TABLE IF NOT EXISTS prospects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_number TEXT UNIQUE,
    owner_name TEXT, owner_type TEXT, owner_priority TEXT,
    courts_search_name TEXT, property_address TEXT,
    total_due REAL, current_due REAL, prior_due REAL,
    appraised_value REAL, homestead INTEGER DEFAULT 0,
    over65 INTEGER DEFAULT 0, veteran INTEGER DEFAULT 0,
    bankruptcy_number TEXT, tax_suit_number TEXT,
    has_suit INTEGER DEFAULT 0, property_url TEXT,
    contact_approach TEXT, added_date TEXT
)""")

count = 0
seen = set()
with open(CSV, encoding='utf-8-sig') as f:
    for row in csv.DictReader(f):
        owner = row.get('Owner','').strip()
        if not owner: continue
        try: total = float(row.get('Total amount due','0').replace(',',''))
        except: total = 0
        if total < 5000: continue
        cs = to_courts(owner)
        if cs in seen: continue
        seen.add(cs)
        otype, opri = classify(owner)
        url = row.get('Property page','')
        acct = url.split('can=')[1].split('&')[0] if 'can=' in url else f"NA_{count}"
        addr_parts = [row.get(f'Address {i}','').strip() for i in range(2,5)]
        addr = next((a for a in addr_parts if a and not a.startswith('%')),'')
        city = row.get('City','')
        contact = "Door knock first" if otype=="individual" else "Formal letter" if otype=="business" else "Contact administrator"
        db.execute("""INSERT OR IGNORE INTO prospects
            (account_number,owner_name,owner_type,owner_priority,courts_search_name,
             property_address,total_due,current_due,prior_due,appraised_value,
             homestead,over65,veteran,bankruptcy_number,has_suit,property_url,
             contact_approach,added_date)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,datetime('now'))""",
            [acct,owner,otype,opri,cs,f"{addr},{city}",total,
             float(row.get('Current amount due','0').replace(',','') or 0),
             float(row.get('Prior amount due','0').replace(',','') or 0),
             float(row.get('Appraised value','0').replace(',','') or 0),
             1 if row.get('Homestead','').strip() else 0,
             1 if row.get('Over 65','').strip() else 0,
             1 if row.get('Veteran','').strip() else 0,
             row.get('Bankruptcy number','').strip(),url])
        count += 1

db.commit()
print(f"Saved {count} prospects to database")

# Show top 10
top = db.execute("SELECT owner_name,owner_type,total_due,property_address FROM prospects ORDER BY total_due DESC LIMIT 10").fetchall()
print("\nTop 10 by balance:")
for r in top:
    print(f"  ${r[2]:>10,.2f} | {r[1].upper()[:3]} | {r[0]} | {r[3][:40]}")
db.close()
