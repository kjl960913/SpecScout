"""Step 2 checklist: the input features a command must be tested on (work/<sub>/features.json).

Items come from four places:
  doc, prior, error, review   written by the agents into work/<sub>/features.json
  leak                         extracted by the script from the program's own output (help text and
                               recorded tests) into work/<sub>/features_auto.json
  sweep                        words a `./pk sweep` found accepted, also in features_auto.json

An item of features.json with the id of an automatic item annotates it (tests, unreachable reason).

Status, decided by the script:
  construct  covered      a linked successful test
             unreachable  `unreachable` reason + at least MIN_FAILED linked failing tests, distinct inputs
                          (a `./pk batch --covers ID` of candidate spellings links them all at once)
  error      covered      a linked test (any outcome: the point is to record the message)
  vocab      covered      a `./pk sweep --covers ID` was run
             unreachable  as for construct
A test is linked to an item by `./pk t --covers ID`, by the item's "tests" list, or (value items with a
known option) by any successful test that passes that value to the option.
"""
import re

import testdb
from common import read_json, sub_dir, write_json, locked
import iface as ifacemod

MIN_FAILED = 8  # distinct failed inputs before an item may be declared unreachable (one ./pk batch)
KINDS = ("construct", "error", "vocab")
AGENT_SOURCES = ("doc", "prior", "error", "review")
ID_RE = re.compile(r"^[^\s]{1,80}$")

# ---------------------------------------------------------------- leak extraction

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
# pest / parser-generator style: "expected literal, raw, not, range, or variable_invocation"
_EXPECTED = re.compile(r"\bexpected(?:\s+one\s+of)?:?\s+((?:`?" + _IDENT + r"`?)(?:\s*,\s*(?:or\s+)?`?" + _IDENT
                       + r"`?|\s+or\s+`?" + _IDENT + r"`?)+)", re.I)
_POSSIBLE = re.compile(r"\[?possible values:\s*([^\]\n]+)\]?", re.I)
_DIDYOUMEAN = re.compile(r"did you mean\s*:?\s*['\"`‘“]([^'\"`’”\s]{1,40})['\"`’”]", re.I)
_UNKNOWN = re.compile(r"\b(unknown|unrecognized|unrecognised|undefined|unsupported|invalid|no such)\s+"
                      r"([A-Za-z][A-Za-z_-]*(?:\s[A-Za-z][A-Za-z_-]*)?)\s*(?::?\s*['\"`‘“<(]|[.!]?\s*$)", re.I)
_OPTION_NEAR = re.compile(r"(--?[A-Za-z0-9][A-Za-z0-9-]*)")
# nouns that belong to the command line parser (covered by the rule tests) or are not closed sets
_NOT_VOCAB = {"argument", "arguments", "option", "options", "flag", "flags", "subcommand", "command", "file",
              "directory", "path", "value", "values", "input", "character", "char", "token", "escape",
              "number", "integer", "float", "utf", "byte", "bytes", "syntax", "expression", "format", "type",
              "length", "range", "line", "lines", "count", "key", "url", "address", "host", "port", "error",
              "usage", "request", "state", "data", "operation", "mode"}
_NOT_RULE = {"eoi", "eof", "end", "a", "an", "the", "one", "of", "or", "and", "to", "be", "but", "found",
             "got", "at", "in", "after", "before", "here"}


def _slug(s):
    return re.sub(r"[^A-Za-z0-9_.=+-]+", "_", s.strip().lower()).strip("_")


def extract(text, origin):
    """Leak items found in one text: [{id, kind, description, evidence, pattern?, option?, value?}]."""
    items = []
    if not text:
        return items
    for line in text.splitlines():
        m = _EXPECTED.search(line)
        if m:
            names = [n.strip("` ") for n in re.split(r"\s*,\s*(?:or\s+)?|\s+or\s+", m.group(1))]
            names = [n for n in names if n and n.lower() not in _NOT_RULE]
            if len(names) >= 2 or any("_" in n for n in names):
                for n in names:
                    items.append({"id": "rule:" + n, "kind": "construct",
                                  "description": f"grammar element `{n}` named by the parser",
                                  "evidence": f"{origin}: {line.strip()[:200]}",
                                  "pattern": m.group(0)})
        m = _POSSIBLE.search(line)
        if m:
            opt = None
            before = line[:m.start()]
            o = _OPTION_NEAR.findall(before)
            if o:
                opt = o[-1]
            for v in re.split(r"\s*,\s*", m.group(1).strip().rstrip(".")):
                v = v.strip().strip("'\"`")
                if not v or len(v) > 40 or " " in v:
                    continue
                iid = f"value:{opt}={v}" if opt else "value:" + v
                items.append({"id": iid, "kind": "construct", "option": opt, "value": v,
                              "description": f"accepted value `{v}`" + (f" of {opt}" if opt else ""),
                              "evidence": f"{origin}: {line.strip()[:200]}"})
        for m in _DIDYOUMEAN.finditer(line):
            w = m.group(1)
            if w.startswith("-"):
                continue
            items.append({"id": "word:" + w, "kind": "construct",
                          "description": f"`{w}`, suggested by the program as an existing name",
                          "evidence": f"{origin}: {line.strip()[:200]}"})
        for m in _UNKNOWN.finditer(line):
            noun = m.group(2).strip().lower()
            if noun.split()[0] in _NOT_VOCAB or noun.split()[-1] in _NOT_VOCAB:
                continue
            items.append({"id": "vocab:" + _slug(noun), "kind": "vocab",
                          "description": f"the set of valid {noun} names (the program rejects unknown ones: "
                                         f"run ./pk sweep with a word list)",
                          "evidence": f"{origin}: {line.strip()[:200]}"})
    return items


# --------------------------------------------------------------------- storage

def auto_path(sub):
    return sub_dir(sub, "features_auto.json")


def agent_path(sub):
    return sub_dir(sub, "features.json")


def add_auto(sub, new_items):
    """Merge new automatic items; returns the ids that were not known before."""
    if not new_items:
        return []
    path = auto_path(sub)
    with locked(path + ".lock"):
        doc = read_json(path, [])
        known = {i["id"] for i in doc}
        added = []
        for it in new_items:
            if it["id"] in known:
                continue
            known.add(it["id"])
            doc.append(dict(it, source=it.get("source", "leak")))
            added.append(it["id"])
        if added:
            write_json(path, doc)
    return added


def scan_record(sub, rec, blobs):
    """Extract leak items from one recorded test (stdout + stderr)."""
    if rec.get("source", "").startswith(("impl", "verify")):
        return []
    items = []
    for stream in ("stdout", "stderr"):
        data = blobs.get(rec.get(stream + "_sha"))
        if data:
            items += extract(data.decode("utf-8", "replace"), f"{rec['id']} {stream}")
    return add_auto(sub, items)


def scan_all(sub, tests, blobs):
    items = extract(ifacemod.help_text(sub), "help")
    added = add_auto(sub, items)
    for r in tests:
        added += scan_record(sub, r, blobs)
    return added


def load_agent(sub):
    """(items, problems) of features.json."""
    path = agent_path(sub)
    try:
        doc = read_json(path)
    except ValueError as e:
        return [], [f"features.json is not valid JSON: {e}"]
    if doc is None:
        return [], ["features.json missing (write the checklist; see goal 2.L)"]
    if isinstance(doc, dict) and isinstance(doc.get("items"), list):
        doc = doc["items"]
    if not isinstance(doc, list):
        return [], ["features.json must be a list of items"]
    errs, seen, out = [], set(), []
    for k, it in enumerate(doc):
        if not isinstance(it, dict):
            errs.append(f"features.json[{k}] must be an object")
            continue
        iid = it.get("id")
        if not isinstance(iid, str) or not ID_RE.match(iid):
            errs.append(f"features.json[{k}]: id must be a short string without spaces")
            continue
        if iid in seen:
            errs.append(f"features.json: id {iid} used twice")
            continue
        seen.add(iid)
        if "tests" in it and not (isinstance(it["tests"], list) and all(isinstance(t, str) for t in it["tests"])):
            errs.append(f"{iid}: tests must be a list of test ids")
            continue
        if it.get("unreachable") is not None and not (isinstance(it["unreachable"], str) and it["unreachable"].strip()):
            errs.append(f"{iid}: unreachable must be a nonempty reason")
            continue
        out.append(it)
    return out, errs


def merged(sub, tests_by_id=None):
    """(items, problems): automatic items annotated by features.json, then the agent's own items."""
    agent, errs = load_agent(sub)
    auto = read_json(auto_path(sub), [])
    by_id = {i["id"]: dict(i) for i in auto}
    order = [i["id"] for i in auto]
    for it in agent:
        iid = it["id"]
        if iid in by_id:
            base = by_id[iid]
            base["tests"] = list(dict.fromkeys((base.get("tests") or []) + (it.get("tests") or [])))
            for f in ("unreachable", "description"):
                if it.get(f):
                    base[f] = it[f]
            continue
        if it.get("kind") not in KINDS:
            errs.append(f"{iid}: kind must be one of {', '.join(KINDS)}")
            continue
        if it.get("source") not in AGENT_SOURCES:
            errs.append(f"{iid}: source must be one of {', '.join(AGENT_SOURCES)}")
            continue
        if not (isinstance(it.get("description"), str) and it["description"].strip()):
            errs.append(f"{iid}: description missing")
            continue
        by_id[iid] = dict(it)
        order.append(iid)
    if tests_by_id is not None:
        for iid in order:
            for t in by_id[iid].get("tests") or []:
                if t not in tests_by_id:
                    errs.append(f"{iid}: unknown test {t}")
    return [by_id[i] for i in order], errs


# ---------------------------------------------------------------------- status

def linked_tests(item, an):
    iid = item["id"]
    out = [t for t in item.get("tests") or [] if t in an.by_id]
    for r in an.tests:
        if iid in (r.get("covers") or []) and r["id"] not in out:
            out.append(r["id"])
    if item.get("option") and item.get("value") is not None:
        for rid, p in an.parsed.items():
            if an.side[rid] == "success" and item["value"] in p["values"].get(item["option"], []) and rid not in out:
                out.append(rid)
    return out


def _counts_as_success(item, rid, an):
    if an.side.get(rid) != "success":
        return False
    pat = item.get("pattern")
    if pat:
        r = an.by_id[rid]
        text = b"".join(an.blobs.get(r.get(s + "_sha")) or b"" for s in ("stdout", "stderr"))
        if pat.encode("utf-8", "surrogateescape") in text:
            return False  # still the same parser error
    return True


def status(item, an):
    """(status, linked test ids, explanation)."""
    linked = linked_tests(item, an)
    kind = item.get("kind")
    if kind == "error":
        if linked:
            return "covered", linked, f"{len(linked)} test(s)"
        return "open", linked, "no test recorded for this error case (./pk t ... --covers ID)"
    if kind == "vocab":
        sweeps = [r["id"] for r in an.tests if r.get("sweep") and item["id"] in (r.get("covers") or [])]
        if sweeps:
            return "covered", linked, f"swept ({len(sweeps)} word tests)"
    else:
        ok = [t for t in linked if _counts_as_success(item, t, an)]
        if ok:
            return "covered", linked, f"{len(ok)} successful test(s), e.g. {ok[0]}"
    failed = [t for t in linked if an.side.get(t) != "success" or not _counts_as_success(item, t, an)]
    distinct = {testdb.input_key(an.by_id[t]) for t in failed}
    reason = item.get("unreachable")
    if reason and len(distinct) >= MIN_FAILED:
        return "unreachable", linked, f"{len(distinct)} failed attempts: {reason[:80]}"
    need = []
    if kind == "vocab":
        need.append("run ./pk sweep ... --covers " + item["id"])
        alt = "or "
    else:
        need.append("add a successful test with --covers " + item["id"] + " (guess its spelling with ./pk batch "
                    "... --covers " + item["id"] + ")")
        alt = "or "
    need.append(f"{alt}record >= {MIN_FAILED} different failing attempts (have {len(distinct)}) and set "
                f"\"unreachable\": \"reason\" in features.json")
    return "open", linked, "; ".join(need)


def evaluate(sub, an):
    """(rows, problems): rows = [(item, status, linked, why)]."""
    items, errs = merged(sub, an.by_id)
    rows = []
    for it in items:
        st, linked, why = status(it, an)
        rows.append((it, st, linked, why))
    return rows, errs


def gate_errors(sub, an):
    rows, errs = evaluate(sub, an)
    out = list(errs)
    opened = [(it, why) for it, st, _, why in rows if st == "open"]
    for it, why in opened[:40]:
        out.append(f"checklist item {it['id']} ({it.get('kind')}, {it.get('source')}) open: {why}")
    if len(opened) > 40:
        out.append(f"... {len(opened) - 40} more open checklist items (./pk features {sub})")
    return out


def report(sub, an, full=False):
    rows, errs = evaluate(sub, an)
    lines = []
    from collections import Counter
    c = Counter(st for _, st, _, _ in rows)
    src = Counter(it.get("source") for it, _, _, _ in rows)
    lines.append(f"checklist (features.json + automatic items): {len(rows)} items — covered {c.get('covered', 0)}, "
                 f"unreachable {c.get('unreachable', 0)}, open {c.get('open', 0)}   sources: "
                 + ", ".join(f"{k} {v}" for k, v in sorted(src.items())))
    for e in errs:
        lines.append("  PROBLEM " + e)
    for it, st, linked, why in rows:
        if st == "open" or full:
            lines.append(f"  [{st}] {it['id']:<34} {it.get('kind', ''):<9} {it.get('source', ''):<6} {why}")
            if st == "open" and it.get("evidence"):
                lines.append(f"         from {it['evidence'][:150]}")
    if not rows:
        lines.append("  (empty: list the input features of this command in features.json — goal 2.L)")
    return "\n".join(lines)


def frozen(sub, an):
    """Checklist snapshot stored in clusters.json at finish."""
    rows, _ = evaluate(sub, an)
    out = []
    for it, st, linked, why in rows:
        keep = {k: it.get(k) for k in ("id", "kind", "source", "description", "evidence", "unreachable")
                if it.get(k) is not None}
        out.append(dict(keep, status=st, tests=[t for t in linked if t in an.by_id]))
    return out
