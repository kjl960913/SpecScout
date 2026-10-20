#!/bin/bash
# scripted step 1: accept the inventory, index every doc for every command
python3 - <<'PY'
import json
d = json.load(open("work/docs_index.draft.json"))
json.dump(d, open("work/docs_index.json", "w"), indent=1)
PY
./pk check 1
