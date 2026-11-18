#!/usr/bin/env bash
# End-to-end demo against a running API (default http://127.0.0.1:8000).
# Replays: import -> publish fails (all missing) -> moisture -> publish ->
# late moisture (stale) -> republish -> correction (stale) -> republish.
set -euo pipefail

BASE="${1:-http://127.0.0.1:8000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EX="$HERE/../examples"

step() { printf '\n=== %s ===\n' "$1"; }

step "1. import 6 blocks (area + fresh weight)"
curl -s -X POST "$BASE/imports" -H 'Content-Type: application/json' -d @"$EX/import.json"

step "2. publish with no moisture recorded -> 422, nothing published"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/publications"

step "3. record moisture for blocks 1-4"
curl -s -X POST "$BASE/moisture" -H 'Content-Type: application/json' -d @"$EX/moisture.json"

step "4. publish: 4 complete blocks, blocks 5-6 pending"
curl -s -X POST "$BASE/publications"

step "5. late moisture for blocks 5-6 -> previous conclusion now stale"
curl -s -X POST "$BASE/moisture" -H 'Content-Type: application/json' -d @"$EX/moisture_late.json"
curl -s "$BASE/publications"

step "6. republish with all 6 blocks"
curl -s -X POST "$BASE/publications"

step "7. correction: block 3 B moisture 0.64 -> 0.70, then republish"
curl -s -X POST "$BASE/corrections" -H 'Content-Type: application/json' -d @"$EX/correction.json"
curl -s -X POST "$BASE/publications"

step "8. publication history (only the newest is current)"
curl -s "$BASE/publications"
echo

step "9. fertilizer: record operation 1 (start weight + plot weighings)"
curl -s -X POST "$BASE/fertilizer/operations" \
  -H 'Content-Type: application/json' -d @"$EX/fertilizer_operation.json"
echo

step "10. fertilizer status: consumptions, refills, merged doses, control pending"
curl -s "$BASE/fertilizer/status"
echo

step "11. approve the flow -> report frozen with provenance"
curl -s -X POST "$BASE/fertilizer/approvals"
echo

step "12. operation 2 has a missing seq 2 -> pending, never treated as zero"
curl -s -X POST "$BASE/fertilizer/operations" \
  -H 'Content-Type: application/json' -d @"$EX/fertilizer_operation_gap.json"
echo
curl -s "$BASE/fertilizer/status"
echo

step "13. late-insert the missing weighing -> neighbours recomputed"
curl -s -X POST "$BASE/fertilizer/corrections" \
  -H 'Content-Type: application/json' -d @"$EX/fertilizer_late_entry.json"
echo

step "14. correct an operation-1 weighing -> old approval now stale"
curl -s -X POST "$BASE/fertilizer/corrections" \
  -H 'Content-Type: application/json' -d @"$EX/fertilizer_correction.json"
echo
curl -s "$BASE/fertilizer/approvals"
echo

step "15. re-approve from the new snapshot"
curl -s -X POST "$BASE/fertilizer/approvals"
echo
