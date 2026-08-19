#!/usr/bin/env python3
"""§33 backfill — recover taxing-unit MEMBERSHIP from the stored petition corpus, by UNION.

WHY UNION AND NOT OVERWRITE. A re-parse is not automatically a better parse. If a fresh extraction
recovers FEWER units than the stored `tax_breakdown`, a blind overwrite SHRINKS the collector set —
and under §33 the set is a LOWER BOUND on who levies, so shrinking it silently makes the payoff
floor lower and more confident at once. "Newer parse = better parse" is the assume-complete defect
wearing one more costume.

So the write is a SUPERSET MERGE:

    new_record = union(stored units, freshly-parsed units)

and it is committed ONLY where it STRICTLY INCREASES coverage. A case where stored is already a
superset is left byte-untouched. By construction this cannot shrink any lower bound.

OFFLINE BY CONSTRUCTION. Reads `data/pdfs/{case}/petition.pdf` + `docket.txt` from the local corpus
(367 dockets / 352 petitions on disk). No portal contact, no API key, no credits — so it is
re-runnable and costs nothing to dry-run. The gds IP block is irrelevant here: this recovers
MEMBERSHIP, not balances. Balances for those collectors resolve when the block lifts, independently.

MATCHING IS A CLOSED VOCABULARY INSIDE A DELIMITED REGION. Units are recognised only if they are
already in `jurisdictions`' roster or already observed in the stored book, only within the petition's
own delimited plaintiff list, and only by longest-match-with-masking. Three earlier designs were
discarded for fabricating collectors; each rejection is documented at the function it killed. A
fabricated collector is worse than a missed one — it becomes a permanent `unavailable` payoff line
for a debt that does not exist.

  --dry-run   (default) measure only, write nothing
  --write     commit the strictly-increasing merges
  --case CN   restrict to one case
"""
import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import jurisdictions as J

ROOT = Path(__file__).parent
DB = ROOT / "data" / "db" / "pcpeak.db"
CORPUS = ROOT / "data" / "pdfs"

# Real-property only — BPP is excluded everywhere else and must be here too.
BASE_WHERE = ("property_type IS NOT 'personal' AND case_track IS NOT 'personal_property'")

# The vocabulary the engine already knows, plus the long forms petitions actually print.
_KNOWN = sorted(set(list(J.ACT_COLLECTED) + list(J.EXTERNAL_COLLECTORS) + list(J._ALIAS)),
                key=len, reverse=True)


def pdf_text(path: Path) -> str:
    try:
        import pypdf
        r = pypdf.PdfReader(str(path))
        return "\n".join(p.extract_text() or "" for p in r.pages)
    except Exception:
        return ""


def parse_units(text: str, vocab) -> set:
    """Taxing units named in the petition text, matched against a CLOSED VOCABULARY.

    ⚠ THIS IS THE SECOND DESIGN. The first tried to DISCOVER units from prose with
    `<NAME> INDEPENDENT SCHOOL DISTRICT` / `CITY OF <NAME>` regexes. Petitions print unit names
    inside running text, so any multi-token capture over-reaches: it minted "ASHLY STEELE RETAINED
    PLAINTIFF DALLAS INDEPENDENT SCHOOL DISTRICT" and, after a stopword pass, "CITY OF DALLAS ACTIVE
    ATTORNEYS". It "improved" 327 of 329 cases, essentially all fabrications.

    A FABRICATED COLLECTOR IS WORSE THAN A MISSED ONE. It enters the payoff as a permanent
    `unavailable` line — a known debt of unknown size that does not exist — and under §33 it is
    precisely a lower bound corrupted upward. So discovery is abandoned: a unit is recognised ONLY if
    it is already in the engine's roster or already observed somewhere in the stored book. Both are
    closed, verified sets, so this cannot invent a collector.

    THE STATED LIMIT: a genuinely novel district, never seen on any case, is NOT discoverable here.
    That failure direction is a no-op on a union, which is the safe one — and it is visible, because
    such a case keeps its existing membership rather than silently gaining a wrong one."""
    region = plaintiff_region(text)
    if not region:
        return None            # NOT "no units" — not deterministically readable. Different thing.
    # LONGEST-MATCH-FIRST WITH MASKING. A short unit name is frequently a PREFIX of a longer one:
    # "DALLAS COUNTY" sits inside "DALLAS COUNTY UTILITY & RECLAMATION DISTRICT". Unmasked substring
    # matching therefore invents the short unit every time the long one appears — measured on
    # TX-24-00079, where it was the ONLY "improvement" the whole run produced, and it was wrong.
    # Consuming each match blanks its span so nothing can match inside an already-identified name.
    found, buf = set(), region
    for canon, surfaces in sorted(vocab.items(), key=lambda kv: -max(len(s) for s in kv[1])):
        for s in sorted(surfaces, key=len, reverse=True):
            k = buf.find(s)
            if k >= 0:
                found.add(canon)
                buf = buf[:k] + ("\x00" * len(s)) + buf[k + len(s):]
                break
    return found


def plaintiff_region(text: str):
    """The petition's own delimited plaintiff list, squeezed — or None if this template has none.

    ⚠ THIRD DESIGN, and the reason for it. Matching the WHOLE document is unsound: every petition
    names Dallas County as the COURT'S VENUE ("IN AND FOR DALLAS COUNTY, TEXAS"), in the deed-records
    citation, and in the boundaries clause. Verified on TX-26-00994 — a Garland ISD-only suit — where
    whole-document matching added DALLAS COUNTY as a taxing unit purely from venue text.

    One template delimits the list exactly:
        NOW COME(S) THE TAXING DISTRICTS SET OUT BELOW: <UNITS> ON BEHALF OF THEMSELVES AND ALL
        TAXING DISTRICTS FOR WHOM THEY COLLECT
    Measured on a 60-petition sample: 19 carry it. The other 41 are a different template where
    ON BEHALF OF belongs to the law-firm authorization clause instead, giving no reliable delimiter —
    so they return None and are reported as NOT DETERMINISTICALLY IMPROVABLE rather than parsed on a
    guess. Their stored `tax_breakdown` came from a Claude extraction that read the Exhibit-A table
    structure, and re-parsing them worse than that is not an improvement.

    ALSO WORTH RECORDING: that clause is the petition stating §33 in its own words — the named
    plaintiff sues "ON BEHALF OF THEMSELVES AND ALL TAXING DISTRICTS FOR WHOM THEY COLLECT". The
    document itself says the named set is not the levying set."""
    sq = re.sub(r"\s+", "", text.upper())
    i = sq.find("SETOUTBELOW")
    if i < 0:
        return None
    j = sq.find("ONBEHALFOF", i)
    if j < 0 or j - i > 600:      # a runaway span means the markers are not bracketing a list
        return None
    return sq[i + len("SETOUTBELOW"):j]


def build_vocabulary(con) -> dict:
    """{canonical_name: {squeezed surface forms}} from the engine roster + every entity string the
    stored book already carries. Closed by construction — nothing outside it can ever be written."""
    surfaces = set(_KNOWN)
    for (tb,) in con.execute(f"SELECT tax_breakdown FROM cases WHERE {BASE_WHERE}"):
        try:
            for row in json.loads(tb or "[]"):
                if isinstance(row, dict) and row.get("entity"):
                    surfaces.add(str(row["entity"]).upper().strip())
        except ValueError:
            continue
    vocab = {}
    for s in surfaces:
        c = J.canonical(s)
        if not c:
            continue
        # Require a reasonably specific surface — a 4-char token would match half the corpus.
        sq = re.sub(r"\s+", "", s.upper())
        if len(sq) >= 10:
            vocab.setdefault(c, set()).add(sq)
        cq = re.sub(r"\s+", "", c.upper())
        if len(cq) >= 10:
            vocab.setdefault(c, set()).add(cq)
    return vocab


def stored_units(tax_breakdown) -> set:
    return {r["collector"] for r in J.petition_collectors(tax_breakdown)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="commit strictly-increasing merges")
    ap.add_argument("--case", help="restrict to one case number")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    q = f"SELECT case_number, tax_breakdown FROM cases WHERE {BASE_WHERE}"
    params = ()
    if args.case:
        q += " AND case_number=?"
        params = (args.case,)
    rows = con.execute(q, params).fetchall()
    vocab = build_vocabulary(con)
    print(f"closed vocabulary: {len(vocab)} canonical units\n")

    improved, unchanged, no_corpus, would_shrink, not_readable = [], 0, [], [], []
    for r in rows:
        cn = r["case_number"]
        pdf = CORPUS / cn / "petition.pdf"
        dock = CORPUS / cn / "docket.txt"
        if not pdf.exists() and not dock.exists():
            no_corpus.append(cn)
            continue
        text = pdf_text(pdf) if pdf.exists() else ""
        if dock.exists():
            try:
                text += "\n" + dock.read_text(errors="ignore")
            except OSError:
                pass
        parsed = parse_units(text, vocab)
        stored = stored_units(r["tax_breakdown"])
        if parsed is None:
            not_readable.append(cn)
            continue
        union = stored | parsed
        # THE INVARIANT, ASSERTED AT THE WRITE: the union can never be smaller than what we hold.
        assert union >= stored, f"{cn}: union shrank the stored set — refusing"
        if parsed and not parsed >= stored:
            # The case the overwrite would have damaged. Recorded, not acted on.
            would_shrink.append((cn, sorted(stored - parsed)))
        if union > stored:
            improved.append((cn, sorted(stored), sorted(union - stored)))
        else:
            unchanged += 1

    print(f"cases considered            : {len(rows)}")
    print(f"no local corpus (skipped)   : {len(no_corpus)}")
    print(f"no delimited plaintiff list : {len(not_readable)}   (other template — NOT deterministically improvable)")
    print(f"stored already a superset   : {unchanged}   (left byte-untouched)")
    print(f"STRICTLY IMPROVED           : {len(improved)}")
    print(f"\nre-parse recovered FEWER units than stored on {len(would_shrink)} case(s) —")
    print(f"exactly what an overwrite would have silently shrunk:")
    for cn, lost in would_shrink[:10]:
        print(f"   {cn}  overwrite would have dropped: {', '.join(lost)}")

    if improved:
        print(f"\n{'case':14s} {'stored':>7s} {'added':>6s}  new units")
        for cn, st, add in improved[:40]:
            print(f"{cn:14s} {len(st):7d} {len(add):6d}  {', '.join(add)}")

    if args.write and improved:
        n = 0
        for cn, st, add in improved:
            row = con.execute("SELECT tax_breakdown FROM cases WHERE case_number=?", (cn,)).fetchone()
            try:
                tb = json.loads(row["tax_breakdown"] or "[]")
            except ValueError:
                tb = []
            have = {J.canonical(x.get("entity")) for x in tb if isinstance(x, dict)}
            # Append ONLY the genuinely new units, with NO amount — an unpriced member is a known
            # debt of unknown size (§33 rule 2), never a fabricated $0.
            for u in add:
                if u not in have:
                    tb.append({"entity": u, "taxAmt": None, "penaltyInterest": None, "total": None})
            con.execute("UPDATE cases SET tax_breakdown=? WHERE case_number=?",
                        (json.dumps(tb), cn))
            n += 1
        con.commit()
        print(f"\nWROTE {n} case(s) — union only, never a decrease.")
    elif improved:
        print("\nDRY RUN — nothing written. Re-run with --write to commit.")


if __name__ == "__main__":
    main()
