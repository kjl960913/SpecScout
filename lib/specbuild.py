"""Step 3: evidence files, unit validation, SPEC rendering, falsification verify, amendments."""
import os
import re
from collections import Counter

import errtemplate
import execute
import testdb
import view
from common import (append_jsonl, display, node, read_json, sub_dir, write_json, write_text)
import iface as ifacemod

MAX_DEFAULT = 20
MAX_PAIRS = 12
MAX_ERR_MEMBERS = 30
MAX_FEATURE_TESTS = 2
MAX_SWEEP_SAMPLES = 6


def opt_file(name):
    return "opt" + re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def env_file(name):
    return "env-" + re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def units(sub):
    """Ordered writing units for one command (from the frozen clusters)."""
    cl = read_json(sub_dir(sub, "clusters.json"))
    if cl is None:
        raise SystemExit(f"{sub}: step 2 not finished (no clusters.json)")
    iface = ifacemod.load(sub)
    rel = cl.get("relations") or []
    out = []
    if any(c["id"] == "errors" for c in cl["clusters"]):
        out.append(("errors", "errors.json"))
    if any(r.get("class") == "II" for r in rel):
        out.append(("exclusions", "exclusions.json"))
    out.append(("default", "default.json"))
    if _described_features(cl):
        out.append(("features", "features.json"))
    for o in iface.get("options", []):
        out.append(("option:" + o["name"], opt_file(o["name"]) + ".json"))
    if any(r.get("class") in ("IV", "I") for r in rel):
        out.append(("combinations", "combinations.json"))
    for e in iface.get("env_vars", []):
        out.append(("env:" + e["name"], env_file(e["name"]) + ".json"))
    out.append(("overview", "overview.json"))
    return out


def _cluster(cl, cid):
    return next((c for c in cl["clusters"] if c["id"] == cid), None)


def _sweep_word(it):
    return it.get("source") == "sweep" and "/" in it["id"]


def _described_features(cl):
    """Checklist items the SPEC must describe: covered or unreachable, sweep words folded into their vocab."""
    return [it for it in cl.get("features") or [] if it["status"] in ("covered", "unreachable") and not _sweep_word(it)]


def _feature_evidence(cl, tests, blobs):
    md, used = [], []
    items = _described_features(cl)
    words = [it for it in cl.get("features") or [] if _sweep_word(it)]
    md.append(f"{len(items)} checklist item(s) to describe (covered or unreachable). For each: its syntax, "
              "what it does and the exact output it produces; for unreachable items what was tried and what the "
              "program answered. One entry may describe several related items (\"ids\": [...]).")
    for it in items:
        md.append(f"\n## {it['id']}  [{it['kind']}, source {it['source']}, {it['status']}]")
        md.append(it.get("description", ""))
        if it.get("evidence"):
            md.append(f"found in: {it['evidence']}")
        if it.get("unreachable"):
            md.append(f"unreachable because: {it['unreachable']}")
        if it["kind"] == "vocab":
            for run in cl.get("sweeps") or []:
                if run.get("covers") != it["id"]:
                    continue
                acc = run.get("accepted") or []
                md.append(f"sweep {run['run']}: {run['words']} words tried (lists {', '.join(run.get('lists') or []) or '-'}"
                          f"{', own list' if run.get('list_files') else ''}), argv template {run.get('argv')}; "
                          f"accepted ({len(acc)}): {' '.join(acc) or '(none)'}")
            mine = [w for w in words if w["id"].startswith(it["id"] + "/")]
            seen, shown = set(), 0
            for w in mine:
                for t in [t for t in w["tests"][:1] if t in tests]:
                    sig = execute.signature(tests[t])
                    if sig not in seen and shown < MAX_SWEEP_SAMPLES:
                        seen.add(sig)
                        shown += 1
                        md.append(view.evidence(tests[t], blobs, 400, 300, title=f"{t} (accepted word)"))
                        used.append(tests[t])
            rej = [t for c in cl["clusters"] if c.get("kind") == "sweep" for t in c["kept"]
                   if tests[t].get("covers") and it["id"] in tests[t]["covers"] and tests[t]["exit"] not in (0, None)]
            for t in rej[:2]:
                md.append(view.evidence(tests[t], blobs, 300, 300, title=f"{t} (rejected word)"))
                used.append(tests[t])
            continue
        linked = [t for t in it.get("tests") or [] if t in tests]
        linked.sort(key=lambda t: (tests[t]["exit"] != 0, t))
        for t in linked[:MAX_FEATURE_TESTS]:
            md.append(view.evidence(tests[t], blobs, 600, 400))
            used.append(tests[t])
        if len(linked) > MAX_FEATURE_TESTS:
            md.append(f"(+{len(linked) - MAX_FEATURE_TESTS} more linked tests: {', '.join(linked[MAX_FEATURE_TESTS:][:20])})")
    return md, used


def feature_assignment(cl, doc):
    """{item id: entry index}, problems."""
    items = _described_features(cl)
    if not isinstance(doc, dict) or not isinstance(doc.get("entries"), list):
        return {}, ["features.json must be {\"entries\": [{\"ids\": [...], \"title\": ..., \"text\": ...}]}"]
    errs, assign = [], {}
    known = {it["id"] for it in cl.get("features") or []}
    for k, e in enumerate(doc["entries"]):
        if not isinstance(e, dict):
            errs.append(f"entries[{k}] must be an object")
            continue
        ids = e.get("ids") or ([e["id"]] if e.get("id") else [])
        if not ids or not all(isinstance(i, str) for i in ids):
            errs.append(f"entries[{k}]: ids must list checklist item ids")
            continue
        if not _nonempty(e.get("text")):
            errs.append(f"entries[{k}]: text missing")
            continue
        for i in ids:
            if i not in known:
                errs.append(f"entries[{k}]: unknown checklist item {i}")
            assign.setdefault(i, k)
    for it in items:
        if it["id"] not in assign:
            errs.append(f"checklist item {it['id']} not described (add it to the ids of an entry)")
    return assign, errs


def _help_lines(sub, spellings):
    text = ifacemod.help_text(sub)
    pat = re.compile(r"(?<![\w-])(" + "|".join(re.escape(s) for s in spellings) + r")(?![\w-])")
    return [l for l in text.splitlines() if pat.search(l)]


def prepare(sub):
    cl = read_json(sub_dir(sub, "clusters.json"))
    iface = ifacemod.load(sub)
    tests = testdb.by_id(sub)
    blobs = execute.Blobs()
    rel = cl.get("relations") or []
    d = sub_dir(sub, "spec_input")
    os.makedirs(d, exist_ok=True)
    os.makedirs(sub_dir(sub, "sections"), exist_ok=True)
    listing = []
    for k, (unit, fname) in enumerate(units(sub), 1):
        md = [f"# {display(node(sub)['path'])} — unit {unit}", f"Write: sections/{fname}", ""]
        used = []
        if unit == "errors":
            c = _cluster(cl, "errors")
            md.append(f"{len(c['kept'])} failing tests, pre-grouped by exact message template "
                      f"({len(c['templates'])} templates). Group them by CAUSE into entries E01, E02, ...")
            extra = []
            for t in c["templates"]:
                md.append(f"\n## {t['id']}  exit {t['exit']} ({t['stream']}), {len(t['members'])} test(s)")
                md.append("```")
                md.append(view.text(t["template"].encode("utf-8", "surrogateescape"), 1500))
                md.append("```")
                for m in t["members"][:MAX_ERR_MEMBERS]:
                    r = tests[m]
                    md.append(f"- {m} [{r['source']}] `{view.argv_str(r['argv'])}`  {view.conditions(r)}"
                              + (f"  — {r['note']}" if r.get("note") else ""))
                if len(t["members"]) > MAX_ERR_MEMBERS:
                    md.append(f"- ... {len(t['members']) - MAX_ERR_MEMBERS} more in spec_input/errors-members.txt")
                    for m in t["members"][MAX_ERR_MEMBERS:]:
                        extra.append(f"{t['id']} {m} {view.argv_str(tests[m]['argv'])}")
            if extra:
                write_text(os.path.join(d, "errors-members.txt"), "\n".join(extra) + "\n")
            md.append("\n(placeholders {argv[i]} stand for the i-th argument of each test)")
        elif unit == "features":
            fmd, fused = _feature_evidence(cl, tests, blobs)
            md += fmd
            used += fused
        elif unit in ("exclusions", "combinations"):
            classes = ("II",) if unit == "exclusions" else ("IV", "I")
            for r in rel:
                cls = r.get("class")
                if cls not in classes:
                    continue
                what = {"II": "mutually exclusive", "IV": f"dominant {r.get('dominant')}",
                        "I": f"{r['a']} acts only together with {r['b']}"}[cls]
                md.append(f"\n## {r['a']} + {r['b']}  ({cls}: {what})  reason given in step 2: {r.get('reason', '')}")
                for role in ("base", "a", "b", "ab", "ba"):
                    tid = (r.get("tests") or {}).get(role)
                    if tid in tests:
                        md.append(view.evidence(tests[tid], blobs, 600, 400, title=f"{role}: {tid}"))
                        used.append(tests[tid])
        elif unit == "default":
            c = _cluster(cl, "default") or {"kept": []}
            reps, seen = [], {}
            for t in c["kept"]:
                s = execute.signature(tests[t])
                seen.setdefault(s, []).append(t)
            for s, ids in seen.items():
                ids.sort(key=lambda i: (not tests[i]["source"].startswith("agent"), i))
                reps.append((ids[0], len(ids)))
            md.append(f"{len(c['kept'])} successful tests without options, {len(seen)} distinct outputs; "
                      f"showing up to {MAX_DEFAULT} representatives.")
            for tid, cnt in reps[:MAX_DEFAULT]:
                md.append(view.evidence(tests[tid], blobs, title=f"{tid} (one of {cnt} with this output)"))
                used.append(tests[tid])
        elif unit.startswith("option:") or unit.startswith("env:"):
            kind, name = unit.split(":", 1)
            c = _cluster(cl, unit) or {"witness": [], "kept": []}
            if kind == "option":
                o = next(o for o in iface["options"] if o["name"] == name)
                spell = [name] + list(o.get("aliases") or [])
                md.append("help lines:")
                md.append("```")
                md += _help_lines(sub, spell) or ["(not mentioned in help)"]
                md.append("```")
            else:
                pairs = []
                for cc in cl["clusters"]:
                    if cc["id"].startswith("env:") and name in cc["id"][4:].split("+"):
                        pairs += cc.get("witness", [])
                c = {"witness": pairs}
            pairs = c.get("witness", []) + [dict(p, dependent=True) for p in c.get("dependent", [])]
            if c.get("context"):
                md.append(f"\n{name} is a CONTEXT option: it only says where input comes from or where output goes. "
                          f"Successful tests using it ({len(c['members'])}; distinct outputs shown, up to 10). "
                          "Describe what it reads/writes and how; its errors are in the errors unit.")
                for t in c["kept"]:
                    md.append(view.evidence(tests[t], blobs, 800, 400))
                    used.append(tests[t])
            elif pairs:
                md.append(f"\n{len(pairs)} witness(es): successful runs whose output differs from the identical "
                          f"run without {name} (control). Distinct output pairs shown, up to {MAX_PAIRS}.")
                seen = set()
                shown = 0
                for p in pairs:
                    s = (execute.signature(tests[p["test"]]), execute.signature(tests[p["control"]]))
                    if s in seen:
                        continue
                    seen.add(s)
                    extra_opts = ""
                    if p.get("dependent"):
                        extra_opts = "  — I relation: the effect is visible only together with the host option in this test"
                    md.append(f"\n### witness {p['test']}  (control {p['control']}){extra_opts}")
                    md.append(view.evidence(tests[p["test"]], blobs))
                    md.append(view.evidence(tests[p["control"]], blobs, 1000, 500, title=f"control {p['control']}"))
                    used += [tests[p["test"]], tests[p["control"]]]
                    shown += 1
                    if shown >= MAX_PAIRS:
                        break
            else:
                md.append(f"\nNo witness: no successful test showed a visible effect of {name}.")
                gap = next((g for g in cl["coverage"]["gaps"] if g[1] == name), None)
                if gap:
                    md.append(f"Step 2 coverage: {gap[2]}")
                md.append("Write the block from help/docs with basis [\"help\"] (or [\"docs\"]); you may run "
                          "./pk verify to try to make the effect visible.")
        else:  # overview
            md.append("Write the overview last, from the help text and the sections you have written.")
            md.append("```")
            md.append(ifacemod.help_text(sub))
            md.append("```")
        if used:
            md.append("\n## Input files used above")
            md.append(view.fixtures(used, blobs))
        path = os.path.join(d, f"{k:02d}-{fname[:-5]}.md")
        write_text(path, "\n".join(md) + "\n")
        listing.append({"unit": unit, "write": "sections/" + fname, "evidence": os.path.relpath(path, sub_dir(sub))})
    write_json(os.path.join(d, "units.json"), listing)
    return listing


# ------------------------------------------------------------- validation

def _load_section(sub, fname):
    path = sub_dir(sub, "sections", fname)
    if not os.path.exists(path):
        return None, [f"sections/{fname} missing"]
    try:
        return read_json(path), []
    except ValueError as e:
        return None, [f"sections/{fname}: invalid JSON: {e}"]


def _nonempty(x):
    return isinstance(x, str) and x.strip()


def error_assignment(cl, doc):
    """test id -> entry id, and problems."""
    errs = []
    c = _cluster(cl, "errors")
    templates = {t["id"]: t["members"] for t in c["templates"]}
    all_tests = set(c["kept"])
    by_template, by_test, entry_of_cause = {}, {}, {}
    if not isinstance(doc, dict) or not isinstance(doc.get("entries"), list):
        return {}, ["errors.json must be {\"entries\": [...]}"]
    ids = set()
    for k, e in enumerate(doc["entries"]):
        eid = e.get("id") if isinstance(e, dict) else None
        if not eid or not re.match(r"^E\d+$", eid) or eid in ids:
            errs.append(f"entries[{k}]: id must be a unique E<number>")
            continue
        ids.add(eid)
        if not _nonempty(e.get("title")):
            errs.append(f"{eid}: title missing")
        causes = e.get("causes")
        if not isinstance(causes, list) or not causes:
            errs.append(f"{eid}: causes must be a nonempty list")
            continue
        for j, cause in enumerate(causes):
            if not isinstance(cause, dict) or not _nonempty(cause.get("text")) or not cause.get("members"):
                errs.append(f"{eid}.causes[{j}]: needs text and members")
                continue
            for m in cause["members"]:
                if m in templates:
                    if m in by_template:
                        errs.append(f"template {m} assigned twice ({by_template[m][0]} and {eid})")
                    by_template[m] = (eid, j)
                elif m in all_tests:
                    if m in by_test:
                        errs.append(f"test {m} listed twice ({by_test[m][0]} and {eid})")
                    by_test[m] = (eid, j)
                else:
                    errs.append(f"{eid}.causes[{j}]: unknown member {m} (use template ids T.. or failing test ids)")
    assign = {}
    for t, members in templates.items():
        if t in by_template:
            for m in members:
                assign[m] = by_template[t]
    assign.update(by_test)
    missing = sorted(all_tests - set(assign))
    if missing:
        errs.append(f"{len(missing)} failing test(s) not assigned to any entry, e.g. {missing[:5]}")
    return assign, errs


def validate(sub, only=None):
    cl = read_json(sub_dir(sub, "clusters.json"))
    iface = ifacemod.load(sub)
    tests = testdb.by_id(sub)
    rel = cl.get("relations") or []
    errs = []
    for unit, fname in units(sub):
        if only and unit != only:
            continue
        doc, e = _load_section(sub, fname)
        if e:
            errs += e
            continue
        p = f"sections/{fname}"
        if unit == "errors":
            errs += [f"{p}: {x}" for x in error_assignment(cl, doc)[1]]
        elif unit == "features":
            errs += [f"{p}: {x}" for x in feature_assignment(cl, doc)[1]]
        elif unit in ("exclusions", "combinations"):
            classes = ("II",) if unit == "exclusions" else ("IV", "I")
            want = {frozenset((r["a"], r["b"])) for r in rel if r.get("class") in classes}
            iv = {frozenset((r["a"], r["b"])) for r in rel if r.get("class") == "IV"}
            got = set()
            for k, e2 in enumerate((doc or {}).get("entries", []) if isinstance(doc, dict) else []):
                if not isinstance(e2, dict) or not e2.get("a") or not e2.get("b") or not _nonempty(e2.get("description")):
                    errs.append(f"{p}: entries[{k}] needs a, b, description")
                    continue
                if frozenset((e2["a"], e2["b"])) in iv and e2.get("dominant") not in (e2["a"], e2["b"]):
                    errs.append(f"{p}: entries[{k}] needs dominant = a or b")
                got.add(frozenset((e2["a"], e2["b"])))
            for pair in want - got:
                errs.append(f"{p}: relation {' + '.join(sorted(pair))} not described")
        elif unit in ("default", "overview"):
            if not isinstance(doc, dict) or not _nonempty(doc.get("text")):
                errs.append(f"{p}: needs a nonempty \"text\"")
        elif unit.startswith("env:"):
            if not isinstance(doc, dict) or not _nonempty(doc.get("text")):
                errs.append(f"{p}: needs var, values, default, precedence, text")
            elif doc.get("var") != unit[4:]:
                errs.append(f"{p}: var must be {unit[4:]}")
        elif unit.startswith("option:"):
            errs += [f"{p}: {x}" for x in _option_errors(sub, unit[7:], doc, tests, cl)]
    return errs


def _option_errors(sub, name, doc, tests, cl):
    errs = []
    if not isinstance(doc, dict) or doc.get("option") != name:
        return [f"\"option\" must be {name}"]
    blocks = doc.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return ["blocks must be a nonempty list"]
    aids, basis_all = {}, set()
    for k, b in enumerate(blocks):
        if not isinstance(b, dict):
            errs.append(f"blocks[{k}] must be an object")
            continue
        for f in ("given", "when", "then"):
            if not _nonempty(b.get(f)):
                errs.append(f"blocks[{k}].{f} missing")
        for a in b.get("assert") or []:
            if not isinstance(a, dict) or not a.get("id") or not _nonempty(a.get("text")):
                errs.append(f"blocks[{k}]: each assert needs id and text")
            elif a["id"] in aids:
                errs.append(f"assert id {a['id']} used twice")
            else:
                aids[a["id"]] = k
        basis = b.get("basis")
        if not isinstance(basis, list) or not basis:
            errs.append(f"blocks[{k}].basis must list test ids (or \"help\"/\"docs\")")
            continue
        for t in basis:
            if t in ("help", "docs"):
                continue
            if t not in tests:
                errs.append(f"blocks[{k}].basis: unknown test {t}")
            basis_all.add((t, k))
    c = _cluster(cl, "option:" + name)
    witnessed = {p["test"] for p in (c or {}).get("witness", []) + (c or {}).get("dependent", [])}
    if (c or {}).get("context"):
        witnessed = set(c["kept"])
    if witnessed and not any(t in witnessed or tests.get(t, {}).get("source", "").startswith("verify:")
                             for t, _ in basis_all):
        errs.append("the option has witnesses; cite at least one witness test in a basis")
    done = Counter()
    for k, v in enumerate(doc.get("verify") or []):
        if not isinstance(v, dict):
            errs.append(f"verify[{k}] must be an object")
            continue
        t, a = v.get("test"), v.get("assert")
        if a not in aids:
            errs.append(f"verify[{k}]: unknown assert {a}")
            continue
        if t not in tests or tests[t].get("source") != f"verify:{name}:{a}":
            errs.append(f"verify[{k}]: {t} is not a ./pk verify test of {name} {a}")
            continue
        done[a] += 1
        if v.get("verdict") not in ("consistent", "inconsistent") or not _nonempty(v.get("reason")):
            errs.append(f"verify[{k}]: verdict consistent|inconsistent and a reason are required")
        if v.get("verdict") == "inconsistent":
            if not v.get("revised"):
                errs.append(f"verify[{k}]: inconsistent assert {a} must be revised (set revised: true)")
            if (t, aids[a]) not in basis_all:
                errs.append(f"verify[{k}]: add {t} to the basis of the block holding {a}")
    for t in tests.values():
        src = t.get("source", "")
        if src.startswith(f"verify:{name}:"):
            a = src.split(":", 2)[2]
            if not any(v.get("test") == t["id"] for v in doc.get("verify") or [] if isinstance(v, dict)):
                errs.append(f"verify test {t['id']} ({a}) has no verdict in \"verify\"")
    return errs


# ------------------------------------------------------------- rendering

def render(sub):
    cl = read_json(sub_dir(sub, "clusters.json"))
    tests = testdb.by_id(sub)
    blobs = execute.Blobs()
    secs = {unit: read_json(sub_dir(sub, "sections", f)) for unit, f in units(sub)}
    iface = ifacemod.load(sub)
    out = [f"# {display(node(sub)['path'])}", "", "## 1. Overview and interface", "",
           secs["overview"]["text"].strip(), "", "## 2. Rejection contract", "", "### Errors", ""]
    index = {}

    def link(tid, entry):
        index.setdefault(tid, [])
        if entry not in index[tid]:
            index[tid].append(entry)

    if "errors" in secs:
        assign, _ = error_assignment(cl, secs["errors"])
        for tid, (eid, _) in assign.items():
            link(tid, f"errors:{eid}")
        for e in secs["errors"]["entries"]:
            members = [t for t, (eid, _) in assign.items() if eid == e["id"]]
            keys = Counter(errtemplate.error_key(tests[t], blobs) for t in members)
            out += [f"#### Error {e['id']}: {e['title'].strip()}", "", "Cause:", ""]
            for j, cause in enumerate(e["causes"]):
                out.append(f"- {cause['text'].strip()}")
            out.append("")
            variants = keys.most_common()
            for k, ((exit_, stream, text), cnt) in enumerate(variants[:40]):
                label = "Message" if len(variants) == 1 else f"Message variant {k + 1} ({cnt} test(s))"
                out += [f"{label} — {stream}, exit status {exit_} (`{{argv[i]}}` = the i-th argument):", "",
                        "```text", text.rstrip("\n"), "```", ""]
            if len(variants) > 40:
                out += [f"({len(variants) - 40} more message variants not shown)", ""]
            out.append("Minimal example(s):")
            out.append("")
            out.append("```sh")
            for j, cause in enumerate(e["causes"]):
                cm = [t for t, (eid, cj) in assign.items() if eid == e["id"] and cj == j]
                if cm:
                    best = min(cm, key=lambda t: (len(" ".join(tests[t]["argv"])), t))
                    out.append(view.argv_str(tests[best]["argv"]))
            out += ["```", ""]
    else:
        out += ["(no failing invocation observed)", ""]
    out += ["### Cannot be combined", ""]
    if "exclusions" in secs:
        for e in secs["exclusions"]["entries"]:
            out.append(f"- `{e['a']}` with `{e['b']}`: {e['description'].strip()}")
            for r in cl.get("relations") or []:
                if r.get("class") == "II" and {r["a"], r["b"]} == {e["a"], e["b"]}:
                    for t in (r.get("tests") or {}).values():
                        link(t, f"exclusions:{e['a']}+{e['b']}")
        out.append("")
    else:
        out += ["(none observed)", ""]
    out += ["## 3. Behaviour", "", "### Default behaviour — zero options", "", secs["default"]["text"].strip(), ""]
    for t in (_cluster(cl, "default") or {}).get("kept", []):
        link(t, "default")
    if "features" in secs:
        out += ["### Input features", ""]
        by_id = {it["id"]: it for it in cl.get("features") or []}
        for e in secs["features"]["entries"]:
            ids = e.get("ids") or [e["id"]]
            title = (e.get("title") or ", ".join(ids)).strip()
            out += [f"#### {title}", "", f"Checklist: {', '.join(ids)}", "", e["text"].strip(), ""]
            for i in ids:
                for t in (by_id.get(i) or {}).get("tests", []):
                    link(t, "feature:" + ids[0])
                for w in cl.get("features") or []:
                    if _sweep_word(w) and w["id"].startswith(i + "/"):
                        for t in w["tests"]:
                            link(t, "feature:" + ids[0])
    for c in cl["clusters"]:
        if c.get("kind") == "sweep":
            for t in c["kept"]:
                if t not in index:
                    link(t, "feature:" + ((tests[t].get("covers") or ["sweep"])[0]))
    out += ["### Option behaviour", ""]
    for o in iface.get("options", []):
        doc = secs["option:" + o["name"]]
        head = o["name"] + (" (" + ", ".join(o.get("aliases") or []) + ")" if o.get("aliases") else "")
        out += [f"#### {head}", ""]
        for b in doc["blocks"]:
            out += [f"Given  {b['given'].strip()}", "", f"When   {b['when'].strip()}", "",
                    f"Then   {b['then'].strip()}", ""]
            if b.get("assert"):
                out.append("Assert:")
                out += [f"- {a['text'].strip()}" for a in b["assert"]]
                out.append("")
            out += [f"Basis: {', '.join(b['basis'])}", ""]
            for t in b["basis"]:
                if t in tests:
                    link(t, "option:" + o["name"])
        c = _cluster(cl, "option:" + o["name"]) or {}
        for t in c.get("kept", []) if c.get("context") else []:
            link(t, "option:" + o["name"])
        for p in c.get("witness", []) + c.get("dependent", []):
            link(p["test"], "option:" + o["name"])
            link(p["control"], "option:" + o["name"])
    out += ["### Combinations", ""]
    if "combinations" in secs:
        for e in secs["combinations"]["entries"]:
            rel_e = next((r for r in cl.get("relations") or [] if r.get("class") in ("IV", "I")
                          and {r["a"], r["b"]} == {e["a"], e["b"]}), {})
            if rel_e.get("class") == "I":
                out.append(f"- `{rel_e['a']}` has an effect only together with `{rel_e['b']}` — {e['description'].strip()}")
            else:
                out.append(f"- `{e['a']}` with `{e['b']}`: `{e.get('dominant')}` takes effect — {e['description'].strip()}")
            for t in (rel_e.get("tests") or {}).values():
                link(t, f"combinations:{e['a']}+{e['b']}")
        out.append("")
    else:
        out += ["(none observed)", ""]
    out += ["### Environment", ""]
    envs = [u for u in secs if u.startswith("env:")]
    if not envs:
        out += ["(no environment variable documented)", ""]
    for u in envs:
        d = secs[u]
        out += [f"#### {d['var']}", ""]
        for f, label in (("values", "Values"), ("default", "Default"), ("precedence", "Precedence")):
            if d.get(f) not in (None, "", []):
                v = d[f] if isinstance(d[f], str) else ", ".join(map(str, d[f]))
                out.append(f"{label}: {v}")
        out += ["", d["text"].strip(), ""]
        for c in cl["clusters"]:
            if c["id"].startswith("env:") and d["var"] in c["id"][4:].split("+"):
                for p in c.get("witness", []):
                    link(p["test"], u)
                    link(p["control"], u)
    return "\n".join(out).rstrip() + "\n", index


def build(sub):
    errs = validate(sub)
    if errs:
        return errs
    text, index = render(sub)
    write_text(sub_dir(sub, "SPEC.md"), text)
    tests = testdb.by_id(sub)
    suite = read_json(sub_dir(sub, "suite.json"))
    verify = sorted(t for t, r in tests.items() if r["source"].startswith("verify:") and execute.stable(r))
    suite["verify"] = verify
    write_json(sub_dir(sub, "suite.json"), suite)
    for t in verify:
        index.setdefault(t, [])
        entry = "option:" + tests[t]["source"].split(":")[1]
        if entry not in index[t]:
            index[t].append(entry)
    for t in suite["tests"]:
        index.setdefault(t, ["unmapped"])
    write_json(sub_dir(sub, "entry_index.json"), index)
    return []


def amend(sub, unit, cite, reason):
    tests = testdb.by_id(sub)
    if cite not in tests:
        return [f"unknown test {cite}"]
    if not execute.stable(tests[cite]):
        return [f"{cite} is not a stable observation"]
    if unit not in dict(units(sub)):
        return [f"unknown unit {unit}; units: " + ", ".join(u for u, _ in units(sub))]
    errs = build(sub)
    if errs:
        return errs
    append_jsonl(sub_dir(sub, "spec_changes.jsonl"), {"unit": unit, "cite": cite, "reason": reason})
    return []
