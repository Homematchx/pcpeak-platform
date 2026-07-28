#!/bin/bash
# sync.command — DOUBLE-CLICK THIS in Finder to push a specific case to prod and verify it.
# macOS runs a .command file in Terminal on double-click, so this is the literal one-click.
# It asks which case number(s) you want to push, shows the plan, pushes, then prints the case's
# live prod fields so you can SEE it landed. Safe to run twice (the push is idempotent).
cd "$(dirname "$0")" || exit 1
echo
echo "Push a case to production"
echo "-------------------------"
read -r -p "Case number(s) to push (space-separated), e.g. TX-23-00569 : " CASES
if [ -z "$CASES" ]; then echo "No case entered — nothing to do."; read -r -p "Press Enter to close. " _; exit 0; fi
echo
python3 sync_all.py $CASES          # plan + push + verify (prints the case's prod fields)
echo
read -r -p "Done. Press Enter to close. " _
