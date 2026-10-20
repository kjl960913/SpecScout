#!/bin/bash
# scripted step 3 for SLUG: minimal valid sections, then build
set -e
S=$1
python3 - "$S" <<'PY'
import json, os, sys
s = sys.argv[1]
w = "work/" + s
cl = json.load(open(w + "/clusters.json"))
units = json.load(open(w + "/spec_input/units.json"))
os.makedirs(w + "/sections", exist_ok=True)
rel = cl.get("relations") or []
for u in units:
    unit, path = u["unit"], w + "/" + u["write"]
    if unit == "errors":
        tpl = [t["id"] for c in cl["clusters"] if c["id"] == "errors" for t in c["templates"]]
        doc = {"entries": [{"id": f"E{k+1:02d}", "title": f"error {t}", "causes": [{"text": "observed", "members": [t]}]} for k, t in enumerate(tpl)]}
    elif unit in ("default", "overview"):
        doc = {"text": "scripted " + unit}
    elif unit == "features":
        ids = [f["id"] for f in cl.get("features") or [] if f["status"] != "open"
               and not (f.get("source") == "sweep" and "/" in f["id"])]
        doc = {"entries": [{"ids": ids, "title": "all", "text": "scripted features"}]}
    elif unit.startswith("option:"):
        name = unit[7:]
        c = next((c for c in cl["clusters"] if c["id"] == unit), {})
        ws = c.get("kept", []) if c.get("context") else [p["test"] for p in c.get("witness", []) + c.get("dependent", [])]
        doc = {"option": name, "blocks": [{"given": "g", "when": name, "then": "t", "assert": [], "basis": ws[:1] or ["help"]}]}
    elif unit.startswith("env:"):
        doc = {"var": unit[4:], "values": "", "default": "", "precedence": "", "text": "scripted"}
    else:
        cls = ("II",) if unit == "exclusions" else ("IV", "I")
        doc = {"entries": [{"a": r["a"], "b": r["b"], "dominant": r.get("dominant", r["a"]), "description": "x"} for r in rel if r["class"] in cls]}
    json.dump(doc, open(path, "w"))
PY
./pk spec build $S
