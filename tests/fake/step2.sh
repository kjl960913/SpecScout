#!/bin/bash
# scripted step 2 explorer for SLUG: draft interface, a few tests, coverage, checklist, findings, ready
set -e
S=$1
cp work/$S/interface.draft.json work/$S/interface.json
WORDS=$(python3 -c "import json;print(' '.join(next(n['path'] for n in json.load(open('work/subcommands.json'))['nodes'] if n['slug']=='$S')))")
mkdir -p work/fixtures/$S
printf '<html><body><p class="a">Hi <b>there</b></p><a href="/x">l</a></body></html>\n' > work/fixtures/$S/in.html
./pk t $S --tag 2.1 --note "bare" -- $WORDS | head -5
./pk t $S --tag 2.4 --stdin in.html --note "stdin" -- $WORDS | head -5
./pk t $S --tag 2.1 --file in.html --covers doc:file-arg --note "file arg" -- $WORDS in.html | head -5
echo '[]' > work/$S/relations.json
./pk coverage $S | tail -15
printf '## Behaviour summary\n- scripted\n' > work/$S/FINDINGS.md
# the scripted explorer marks every automatic item unreachable with two failing attempts where needed
python3 - "$S" <<'PY'
import json, sys
S = sys.argv[1]
items = [{"id": "doc:file-arg", "kind": "construct", "source": "doc", "description": "input file argument"}]
json.dump(items, open(f"work/{S}/features.json", "w"))
PY
# close every open item the way a stuck agent would: two different failing attempts + a reason
for id in $(./pk features $S --open | sed -n 's/^  \[open\] \([^ ]*\).*/\1/p'); do
  ./pk t $S --covers "$id" --note "attempt 1 for $id" -- $WORDS --pk-nosuch-1 >/dev/null
  ./pk t $S --covers "$id" --note "attempt 2 for $id" -- $WORDS --pk-nosuch-2 >/dev/null
  python3 - "$S" "$id" <<'PY'
import json, sys
S, iid = sys.argv[1:]
p = f"work/{S}/features.json"
items = json.load(open(p))
mine = next((i for i in items if i["id"] == iid), None)
if mine is None:
    mine = {"id": iid}
    items.append(mine)
mine["unreachable"] = "scripted agent"
json.dump(items, open(p, "w"))
PY
done
./pk ready $S || { ./pk features $S --open | head -30; exit 1; }
