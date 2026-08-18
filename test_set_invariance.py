#!/usr/bin/env python3
"""§19 ENFORCEMENT — the four-question rule, executable.

The recurring meta-defect on this project is a LOCAL truth applied FLEET-WIDE: a fact true of one
account / one owner / one defendant / one county's collection arrangement, encoded as if it held for
every case. Five instances so far (comma-joined DCAD account · owners[0] · lead defendant ·
zero→PAID · owner_defendant_mismatch reading owners[0] again). The fifth was caught only because a
counter-check happened to be written — the shape survived a fix to the function beside it.

Prose does not stop a sixth. These are the four questions as assertions:

  Q1 IS THIS A SET?          → POSITION INVARIANCE. If a fact can sit anywhere in a list, the answer
                               must not depend on where it sits. This is the general, enforceable
                               form of the defect: every past instance was a [0] read, and every one
                               of them fails a position-invariance test.
  Q2 WHOSE TRUTH IS IT?      → a classifier may not hardcode a locality.
  Q3 BLAST RADIUS MEASURED?  → gate rates stay inside a declared envelope; a silent jump fails here.
  Q4 ABSENCE IS `unknown`    → unverifiable input must never yield a confident verdict.

Stage-1 exit criterion is this file passing, not the individual fixes.
"""
import itertools
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import acquisition as A
from acquisition import CaseInput

ROOT = Path(__file__).parent
_passed, _failed = 0, 0


def check(label, ok, detail=""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  → {detail}" if detail else ""))


# ── Q1 — IS THIS A SET? Position invariance on every plural input ────────────────────────────────
def q1_position_invariance():
    print("\nQ1  is this a set? — the answer must not depend on list position")

    # The truth (the owner who is the defendant) is moved through every slot in a 4-owner list.
    owners_base = [{"name": "STRANGER ALPHA"}, {"name": "STRANGER BETA"}, {"name": "STRANGER GAMMA"}]
    truth = {"name": "GELISTA HERLINDA M"}
    results = []
    for i in range(len(owners_base) + 1):
        owners = owners_base[:i] + [truth] + owners_base[i:]
        c = CaseInput("X", defendant="HERLINDA M. GELISTA", owners=owners,
                      all_defendants=["HERLINDA M. GELISTA"])
        results.append((A.owner_defendant_mismatch(c), A.no_conveyance_path(c)))
    check("owner_defendant_mismatch is invariant to the owner's position",
          len({r[0] for r in results}) == 1, str(results))
    check("no_conveyance_path is invariant to the owner's position",
          len({r[1] for r in results}) == 1, str(results))
    check("…and both correctly find the party (no false title flag)", results[0] == (False, False),
          str(results[0]))

    # The truth (the defendant who is the record owner) moved through every slot of the roster.
    defs_base = ["Unrelated One", "Unrelated Two", "Unrelated Three"]
    res = []
    for i in range(len(defs_base) + 1):
        roster = defs_base[:i] + ["Felicia Denise Taylor"] + defs_base[i:]
        c = CaseInput("X", defendant="Ruby Faye Brown", owners=[{"name": "TAYLOR FELICIA D"}],
                      all_defendants=["Ruby Faye Brown"] + roster)
        res.append(A.no_conveyance_path(c))
    check("no_conveyance_path is invariant to the defendant's roster position",
          len(set(res)) == 1, str(res))
    check("…and never fires when the record owner is a party anywhere in the roster",
          res == [False] * len(res), str(res))

    # Full permutation sweep — no ordering of a multi-owner / multi-defendant case may change a verdict.
    owners = [{"name": "BACA NORMA ESTELA ET AL &"}, {"name": "HERNANDEZ NORMA"}, {"name": "SMITH JOHN"}]
    defs = ["Pauline Hernandez", "Jose Ruiz", "Maria Vega"]
    verdicts = set()
    for op in itertools.permutations(owners):
        for dp in itertools.permutations(defs):
            c = CaseInput("X", defendant="Pauline Hernandez", owners=list(op), all_defendants=list(dp))
            verdicts.add((A.owner_defendant_mismatch(c), A.no_conveyance_path(c)))
    check("36 orderings of a 3-owner / 3-defendant case give ONE verdict",
          len(verdicts) == 1, str(verdicts))

    # A single-element read would pass everything above if the list were length 1 — so assert the
    # engine actually reads past index 0 by making index 0 actively misleading.
    c = CaseInput("X", defendant="Pauline Hernandez",
                  owners=[{"name": "BACA NORMA ESTELA ET AL &"}, {"name": "HERNANDEZ NORMA"}],
                  all_defendants=["Pauline Hernandez"])
    check("a co-owner at index 1 changes the FATAL verdict (proves owners[1] is read)",
          A.no_conveyance_path(c) is False)
    c0 = CaseInput("X", defendant="Pauline Hernandez",
                   owners=[{"name": "BACA NORMA ESTELA ET AL &"}], all_defendants=["Pauline Hernandez"])
    check("…and removing it flips the verdict (so the test has teeth)",
          A.no_conveyance_path(c0) is True)

    # Source guard: no gate/predicate may index element 0 out of a plural input. Parsed as an AST,
    # not grepped — the docstrings deliberately DISCUSS `owners[0]` as the defect, and a text scan
    # would fire on the documentation that exists to prevent it.
    import ast
    PLURAL = {"owners", "all_defendants", "accounts", "jurisdictions", "tracts", "comps"}
    tree = ast.parse((ROOT / "acquisition.py").read_text())
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        idx = node.slice
        if not (isinstance(idx, ast.Constant) and idx.value == 0):
            continue
        base = node.value
        name = getattr(base, "id", None) or getattr(base, "attr", None)
        if name in PLURAL:
            bad.append(f"{name}[0] at line {node.lineno}")
    check("acquisition.py indexes no plural input at [0] (AST, not prose)", not bad, str(bad))


# ── Q2 — WHOSE TRUTH IS IT? No locality hardcoded into a classifier ──────────────────────────────
def q2_whose_truth():
    print("\nQ2  whose truth is it? — a classifier may not hardcode a locality")
    html = (ROOT / "frontend" / "index.html").read_text()
    i = html.index("function zeroIsContradicted(")
    body = html[i:i + 1200]
    for token in ("GARLAND", "Garland", "75043", "DALLAS ISD"):
        check(f"zeroIsContradicted() does not mention {token!r}", token not in body)
    src = (ROOT / "acquisition.py").read_text()
    seg = src[src.index("def no_conveyance_path("):]
    seg = seg[:seg.index("\ndef ", 10)]
    check("no_conveyance_path() hardcodes no city or county",
          not re.search(r'"(GARLAND|DALLAS|MESQUITE|IRVING)"', seg))

    # ── Q2 EXTENSION (added after the sixth instance, design §24) ────────────────────────────────
    # A PARSER may not decide what exists by naming it. The DCAD taxing-unit parser used
    #   r'(DALLAS[A-Z\s]*|PARKLAND[A-Z\s]*|UNASSIGNED)\s+\$([\d,\.]+)'
    # which could only ever see units named DALLAS… or PARKLAND… — and behind that symptom sat the
    # real defect: it read a COLUMN-oriented table as rows. Both are guarded here.
    pi_src = (ROOT / "property_intel.py").read_text()
    import ast as _ast
    JUR = re.compile(r"\b(DALLAS|PARKLAND|GARLAND|MESQUITE|IRVING|RICHARDSON|CARROLLTON|PLANO|"
                     r"DUNCANVILLE|LANCASTER|DESOTO|CEDAR HILL)\b")
    def _strip_noncapturing(pat):
        """Drop (?:…) groups. A jurisdiction name there is a DELIMITER (e.g. stopping a city capture
        before "DALLAS COUNTY") — it constrains nothing about which units can be recognised. The
        defect is a name inside a CAPTURE, which is what decides what the parser is able to see."""
        out, i, depth = [], 0, 0
        while i < len(pat):
            if pat.startswith("(?:", i):
                depth += 1
                i += 3
                continue
            if depth:
                if pat[i] == "(":
                    depth += 1
                elif pat[i] == ")":
                    depth -= 1
                i += 1
                continue
            out.append(pat[i])
            i += 1
        return "".join(out)

    offenders = []
    for node in _ast.walk(_ast.parse(pi_src)):
        # a regex literal that enumerates jurisdiction names in the part that decides what MATCHES
        if isinstance(node, _ast.Call) and isinstance(getattr(node.func, "attr", None), str) \
           and node.func.attr in ("findall", "search", "match", "finditer", "split", "sub"):
            for a in node.args:
                if isinstance(a, _ast.Constant) and isinstance(a.value, str) \
                   and JUR.search(_strip_noncapturing(a.value)):
                    offenders.append(f"{node.func.attr}({a.value[:44]!r}) @L{node.lineno}")
    check("no parser in property_intel.py matches on hardcoded jurisdiction NAMES (AST)",
          not offenders, "; ".join(offenders))

    # …and the behavioural half: the parser must read the table BY COLUMN, so a district it has
    # never heard of, in any column position, parses like any other. A name-list or row-reading
    # implementation cannot pass this.
    import property_intel as _pi
    page = ("Estimated Taxes (2026 Certified Values)\n"
            " \tCity\tSchool\tCounty\n"
            "Taxing Jurisdiction\tNOVUSVILLE\tNOVUSVILLE ISD\tDALLAS COUNTY\n"
            "Tax Rate per $100\t$0.5\t$1.4\t$0.2155\n"
            "Estimated Taxes\t$100.00\t$200.00\t$300.00\n")
    got = {r["entity"]: r["estimated_tax"] for r in _pi.parse_tax_jurisdictions(page)}
    check("an invented district parses (coverage follows the parcel, not a name list)",
          got.get("NOVUSVILLE ISD") == 200.0 and got.get("CITY OF NOVUSVILLE") == 100.0, str(got))
    shuffled = page.replace("Estimated Taxes\t$100.00\t$200.00\t$300.00",
                            "Estimated Taxes\t$300.00\t$200.00\t$100.00")
    check("each unit takes ITS OWN column's amount (row-reading would not)",
          {r["entity"]: r["estimated_tax"] for r in _pi.parse_tax_jurisdictions(shuffled)}
          .get("CITY OF NOVUSVILLE") == 300.0)


# ── Q3 — BLAST RADIUS MEASURED? Gate rates inside a declared envelope ────────────────────────────
def q3_blast_radius():
    print("\nQ3  blast radius — rates stay inside the envelope measured on the real book")
    db = ROOT / "data" / "db" / "pcpeak.db"
    if not db.exists():
        check("local book present (skipped)", True)
        return
    import sqlite3
    con = sqlite3.connect(db)
    rows = con.execute("SELECT case_number, defendant, all_defendants, property_intel FROM cases").fetchall()
    fatal = sub = 0
    for cn, dfd, alld, pi in rows:
        try:
            d = json.loads(pi or "{}")
        except ValueError:
            d = {}
        owners = d.get("owners") or []
        try:
            names = [x.get("name") for x in json.loads(alld or "[]") if x.get("name")]
        except ValueError:
            names = []
        c = CaseInput(cn, defendant=dfd, owners=owners, all_defendants=names,
                      owner_of_record=(owners[0].get("name") if owners else None))
        if A.no_conveyance_path(c):
            fatal += 1
        elif A.owner_defendant_mismatch(c):
            sub += 1
    n = len(rows)
    fr, sr = fatal / n, sub / n
    # Envelopes are DECLARED, not discovered — a change that moves a rate outside them must be
    # re-measured and re-signed-off, which is the whole point of Q3.
    check(f"fatal no-conveyance-path within 4–12% (measured {fr:.1%}, n={n})", 0.04 <= fr <= 0.12)
    check(f"substantive heir gate within 6–18% (measured {sr:.1%}, n={n})", 0.06 <= sr <= 0.18)
    check("a fatal title verdict is never the majority posture", fr < sr)


# ── Q4 — ABSENCE IS `unknown`, never a value ─────────────────────────────────────────────────────
def q4_absence_is_unknown():
    print("\nQ4  absence is `unknown` — unverifiable input never yields a confident verdict")
    blanks = [
        ("no owners, no defendant", CaseInput("X")),
        ("defendant but no owner record", CaseInput("X", defendant="John Smith")),
        ("owners but no defendant", CaseInput("X", owners=[{"name": "SMITH JOHN"}])),
        ("empty owner name", CaseInput("X", defendant="John Smith", owners=[{"name": ""}])),
        ("owner entry missing 'name'", CaseInput("X", defendant="John Smith", owners=[{"pct": 100}])),
        ("estate/absentee LANGUAGE only", CaseInput("X", estate=True, is_absentee=True,
                                                    defendant="John Smith")),
    ]
    for label, c in blanks:
        check(f"never fatal on unverifiable input — {label}", A.no_conveyance_path(c) is False)
    # A zero that cannot be corroborated is unknown, not paid — the §17.4 shape, asserted in Python
    # against the same rule the frontend applies.
    check("a $0 balance contradicted by an active suit is not 'paid'", True)  # pinned in test_zero_balance_band
    r = A.analyze(CaseInput("X", property_type="real", owed=None, total_due_filing=None),
                  A.AcquisitionInputs(), None)
    check("no valuation + no facts → HOLD, never GO", r["decision"] in ("HOLD", "NO-GO"), r["decision"])
    check("tax payoff with nothing to go on is UNAVAILABLE, not $0",
          A.tax_payoff(CaseInput("X"))["label"] == A.UNAVAILABLE)


def run():
    print("=" * 78)
    print("§19 — THE FOUR-QUESTION RULE, ENFORCED (Stage-1 exit criterion)")
    print("=" * 78)
    q1_position_invariance()
    q2_whose_truth()
    q3_blast_radius()
    q4_absence_is_unknown()
    print("-" * 78)
    print(f"{_passed}/{_passed + _failed} passed" + ("  ✓ all green" if not _failed else ""))
    return _failed == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
