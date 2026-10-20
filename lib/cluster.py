"""Grouping of a command's tests, controls, witnesses, coverage and filtering (step 2.7)."""
import json
import os
from collections import Counter, OrderedDict

import argvparse
import errtemplate
import execute
import features as featmod
import iface as ifacemod
import sweep as sweepmod
import testdb
from common import node, read_json, sub_dir, write_json


def _side(rec, nonerror):
    if not execute.stable(rec):
        return "unstable"
    if rec.get("tui_running"):
        return "success"  # an interactive program still running after the last key
    return "success" if rec["exit"] == 0 or rec["exit"] in nonerror else "error"


def _opts_items(parsed, drop=None):
    items = []
    for name, count in sorted(parsed["options"].items()):
        if name == drop:
            continue
        items.append([name, parsed["values"].get(name, []), count])
    return items


def _ctx(rec, parsed, env, opts_items):
    return json.dumps([rec["channel"], rec.get("tui"), rec.get("files") or {}, rec.get("stdin"), rec.get("setup") or [],
                       sorted((env or {}).items()), parsed["positionals"], parsed["separator"], opts_items],
                      sort_keys=True)


def group_of(rec, parsed, context=()):
    """Group of a successful test. Context options (input/output locations) do not count."""
    env = rec.get("env") or {}
    if env:
        return "env:" + "+".join(sorted(env))
    if not parsed["parse_ok"] or parsed["unknown"] or parsed["missing_value"]:
        return "misc"
    opts = sorted(set(parsed["options"]) - set(context))
    if not opts:
        return "default"
    if len(opts) == 1:
        return "option:" + opts[0]
    return "combo:" + "+".join(opts)


class Analysis:
    """Everything derived from one command's test store."""

    def __init__(self, sub, blobs=None):
        self.sub = sub
        self.blobs = blobs or execute.Blobs()
        self.iface = ifacemod.load(sub) or ifacemod.draft(sub)
        self.root_iface = ifacemod.load_root_globals(sub)
        self.path = node(sub)["path"]
        self.nonerror = set(self.iface.get("nonerror_exits") or [])
        self.context = {o["name"] for o in self.iface.get("options", []) + (self.root_iface or {}).get("options", [])
                        if o.get("context")}
        self.reload()

    # ------------------------------------------------------------ indexing
    def reload(self):
        self.tests = [r for r in testdb.load(self.sub) if not r["source"].startswith("impl")]
        self.by_id = {r["id"]: r for r in self.tests}
        self.parsed, self.side, self.group = {}, {}, {}
        self.index = {}
        for r in self.tests:
            p = argvparse.parse(r["argv"], self.path, self.iface, self.root_iface)
            self.parsed[r["id"]] = p
            self.side[r["id"]] = _side(r, self.nonerror)
            self.group[r["id"]] = (group_of(r, p, self.context) if self.side[r["id"]] == "success"
                                   else self.side[r["id"]])
            if r.get("sweep") and self.side[r["id"]] != "unstable":
                self.group[r["id"]] = "sweep:" + r["sweep"]["run"]
            if self.side[r["id"]] != "unstable" and p["parse_ok"]:
                key = _ctx(r, p, r.get("env"), _opts_items(p))
                self.index.setdefault(key, r["id"])

    def opts(self, rid):
        """Behaviour options of a test: its option set without context options."""
        return set(self.parsed[rid]["options"]) - self.context

    def control_key(self, rid, option=None, env_var=None):
        r, p = self.by_id[rid], self.parsed[rid]
        env = dict(r.get("env") or {})
        if env_var:
            env.pop(env_var, None)
        return _ctx(r, p, env, _opts_items(p, drop=option))

    def find_control(self, rid, option=None, env_var=None):
        return self.index.get(self.control_key(rid, option, env_var))

    # ---------------------------------------------------------- candidates
    def witness_targets(self):
        """(test id, option or None, env var or None) pairs that need a control."""
        out = []
        for rid, g in self.group.items():
            if self.side[rid] != "success":
                continue
            if g.startswith("option:"):
                out.append((rid, g[len("option:"):], None))
            elif g.startswith("env:"):
                for var in sorted(self.by_id[rid].get("env") or {}):
                    out.append((rid, None, var))
        return out

    def missing_controls(self):
        """Control tests to execute: same conditions without the option / variable."""
        todo, seen = [], set()
        known = {testdb.input_key(r) for r in self.tests}
        for rid, opt, var in self.witness_targets():
            if self.find_control(rid, opt, var):
                continue
            r = self.by_id[rid]
            t = testdb.canonical(r)
            if opt:
                argv = argvparse.remove_option(r["argv"], self.parsed[rid], opt)
                if argv is None:
                    continue
                t["argv"] = argv
                note = f"control for {rid}: without {opt}"
            else:
                t["env"] = {k: v for k, v in t["env"].items() if k != var}
                note = f"control for {rid}: without {var}"
            k = testdb.input_key(t)
            if k in known or k in seen:
                continue
            seen.add(k)
            todo.append((t, "rule:control", note, {"control_for": rid}))
        return todo

    def run_missing_controls(self, binary):
        todo = self.missing_controls()
        if todo:
            testdb.run_and_record(self.sub, binary, todo)
            self.reload()
        return len(todo)

    # ------------------------------------------------------------ witness
    def _solo(self):
        """Single-option / single-variable witnesses: {('option', A) | ('env', X): {...}}"""
        out = {}
        for o in self.iface.get("options", []):
            out[("option", o["name"])] = {"witness": [], "inert": [], "no_control": []}
        for e in self.iface.get("env_vars", []):
            out[("env", e["name"])] = {"witness": [], "inert": [], "no_control": []}
        for rid, opt, var in self.witness_targets():
            subject = ("option", opt) if opt else ("env", var)
            slot = out.setdefault(subject, {"witness": [], "inert": [], "no_control": []})
            cid = self.find_control(rid, opt, var)
            if cid is None or cid == rid:
                slot["no_control"].append(rid)
            elif not execute.same_output(self.by_id[rid], self.by_id[cid]):
                slot["witness"].append((rid, cid))
            else:
                slot["inert"].append(rid)
        return out

    def _check_i(self, rel, solo):
        """Problems with one relation of class I (A depends on B), [] when it satisfies the definition:
        A has no witness of its own, B has one, and A+B succeeds with output different from B alone."""
        a, b, tests = rel.get("a"), rel.get("b"), rel.get("tests") or {}
        errs = []
        for role in ("a", "b", "ab"):
            if tests.get(role) not in self.by_id:
                errs.append(f"I {a} on {b}: tests.{role} missing")
        if errs:
            return errs
        ta, tb, tab = tests["a"], tests["b"], tests["ab"]
        if self.opts(ta) != {a}:
            errs.append(f"I {a} on {b}: tests.a ({ta}) must use exactly {a}")
        if self.opts(tb) != {b} or self.side[tb] != "success":
            errs.append(f"I {a} on {b}: tests.b ({tb}) must be a successful run with exactly {b}")
        if self.opts(tab) != {a, b} or self.side[tab] != "success":
            errs.append(f"I {a} on {b}: tests.ab ({tab}) must be a successful run with exactly {a} and {b}")
        if errs:
            return errs
        if solo.get(("option", a), {}).get("witness"):
            errs.append(f"I {a} on {b}: {a} already has a witness of its own, so it is not dependent")
        if not solo.get(("option", b), {}).get("witness"):
            errs.append(f"I {a} on {b}: {b} has no witness of its own")
        if self.control_key(tab, option=a) != self.control_key(tb):
            errs.append(f"I {a} on {b}: {tab} must be {tb} plus {a}, everything else identical")
        elif execute.same_output(self.by_id[tab], self.by_id[tb]):
            errs.append(f"I {a} on {b}: {tab} has the same output as {tb}")
        return errs

    def witnesses(self):
        """Solo witnesses plus, for options with none, dependent witnesses from valid I relations."""
        out = self._solo()
        for slot in out.values():
            slot["dependent"], slot["hosts"] = [], []
        for r in self.relations():
            if not isinstance(r, dict) or r.get("class") != "I" or self._check_i(r, out):
                continue
            slot = out.get(("option", r["a"]))
            if slot is None:
                continue
            slot["dependent"].append((r["tests"]["ab"], r["tests"]["b"]))
            if r["b"] not in slot["hosts"]:
                slot["hosts"].append(r["b"])
        return out

    def is_witness(self, rid, option):
        if self.side.get(rid) != "success" or option not in self.parsed[rid]["options"]:
            return None
        cid = self.find_control(rid, option)
        if cid and cid != rid and not execute.same_output(self.by_id[rid], self.by_id[cid]):
            return cid
        return None

    # ------------------------------------------------------------- errors
    def error_templates(self, exclude=()):
        groups = OrderedDict()
        for rid, side in self.side.items():
            if side != "error" or rid in exclude:
                continue
            key = errtemplate.error_key(self.by_id[rid], self.blobs)
            groups.setdefault(key, []).append(rid)
        ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[1][0]))
        out = []
        for k, ((exit_, stream, text), members) in enumerate(ordered, 1):
            out.append({"id": f"T{k:02d}", "exit": exit_, "stream": stream, "template": text,
                        "members": members,
                        "options_seen": sorted({tuple(sorted(self.parsed[m]["options"])) for m in members})})
        return out

    # ---------------------------------------------------------- relations
    def relations(self):
        path = sub_dir(self.sub, "relations.json")
        rel = read_json(path, [])
        return rel if isinstance(rel, list) else []

    def relation_errors(self):
        errs = []
        path = sub_dir(self.sub, "relations.json")
        if not os.path.exists(path):
            return ["relations.json missing (write [] if no I/II/IV pair was found)"]
        try:
            rel = read_json(path)
        except ValueError as e:
            return [f"relations.json is not valid JSON: {e}"]
        if not isinstance(rel, list):
            return ["relations.json must be a list"]
        for k, r in enumerate(rel):
            if not isinstance(r, dict):
                errs.append(f"[{k}] must be an object")
                continue
            if r.get("class") not in ("I", "II", "IV"):
                errs.append(f"[{k}] class must be I, II or IV")
            if not r.get("a") or not r.get("b"):
                errs.append(f"[{k}] needs option names a and b")
            if r.get("class") == "IV" and r.get("dominant") not in (r.get("a"), r.get("b")):
                errs.append(f"[{k}] IV needs dominant = a or b")
            tests = r.get("tests")
            if not isinstance(tests, dict) or not tests:
                errs.append(f"[{k}] tests must map base/a/b/ab/ba to test ids")
                continue
            for role, tid in tests.items():
                if tid not in self.by_id:
                    errs.append(f"[{k}] tests.{role}: unknown test id {tid}")
        if not errs:
            solo = self._solo()
            for r in rel:
                if r.get("class") == "I":
                    errs += self._check_i(r, solo)
        return errs

    # ----------------------------------------------------------- coverage
    def coverage(self, wit=None):
        wit = wit or self.witnesses()
        appears = Counter()
        appears_ok = Counter()
        for rid, p in self.parsed.items():
            for name in p["options"]:
                appears[name] += 1
                if self.side[rid] == "success":
                    appears_ok[name] += 1
            for var in self.by_id[rid].get("env") or {}:
                appears["$" + var] += 1
                if self.side[rid] == "success":
                    appears_ok["$" + var] += 1
        rows, gaps = [], []
        for kind, name in wit:
            w = wit[(kind, name)]
            key = name if kind == "option" else "$" + name
            if kind == "option" and name in self.context:
                if appears_ok[key]:
                    rows.append((kind, name, "triggered (context option, used in successful tests)", appears_ok[key]))
                else:
                    why = "context option never used in a successful test"
                    rows.append((kind, name, "gap", why))
                    gaps.append((kind, name, why))
                continue
            if w["witness"]:
                rows.append((kind, name, "triggered", len(w["witness"])))
                continue
            if w.get("dependent"):
                rows.append((kind, name, "triggered only with " + ", ".join(w["hosts"]), len(w["dependent"])))
                continue
            if not appears[key]:
                why = "no test"
            elif not appears_ok[key]:
                why = "appears only in failing or unstable tests"
            elif w["inert"]:
                why = (f"{len(w['inert'])} test(s), all identical to their controls (inert); if it only acts "
                       f"together with another option, record that as an I relation")
            elif w["no_control"]:
                why = "succeeds only together with other options/variables (no single-option control)"
            else:
                why = "succeeds only together with other options/variables"
            rows.append((kind, name, "gap", why))
            gaps.append((kind, name, why))
        return rows, gaps

    # ------------------------------------------------------------- filter
    def clusters(self):
        wit = self.witnesses()
        rel = self.relations()
        rel_ids = {t for r in rel for t in (r.get("tests") or {}).values() if t in self.by_id}
        groups = OrderedDict()
        for rid in self.by_id:
            groups.setdefault(self.group[rid], []).append(rid)
        kept, out = set(), []
        sweep_kept = set()
        for run, recs in sweepmod.runs(self.tests).items():
            recs = [r for r in recs if self.side[r["id"]] != "unstable"]
            if recs:
                sweep_kept.update(sweepmod.kept_ids(recs, self.blobs, self.nonerror))
        for gid in sorted(groups, key=lambda g: (g.split(":")[0] != "default", g)):
            members = groups[gid]
            if gid in ("unstable", "misc", "error"):
                continue
            if gid.startswith("sweep:"):
                c = {"id": gid, "kind": "sweep", "members": members,
                     "kept": [t for t in members if t in sweep_kept]}
                c["signatures"] = len({execute.signature(self.by_id[t]) for t in c["kept"]})
                kept.update(c["kept"])
                out.append(c)
                continue
            c = {"id": gid, "kind": gid.split(":")[0], "members": members}
            if gid == "default":
                c["kept"] = list(members)
            elif gid.startswith("option:") or gid.startswith("env:"):
                pairs = []
                if gid.startswith("option:"):
                    w = wit.get(("option", gid[len("option:"):]), {})
                    pairs = w.get("witness", [])
                    c["dependent"] = [{"test": t, "control": cc} for t, cc in w.get("dependent", [])]
                else:
                    for var in gid[len("env:"):].split("+"):
                        pairs += wit.get(("env", var), {}).get("witness", [])
                    pairs = [p for p in pairs if p[0] in members]
                c["witness"] = [{"test": t, "control": cc} for t, cc in pairs]
                dep = [(d["test"], d["control"]) for d in c.get("dependent", [])]
                c["kept"] = sorted({x for p in pairs + dep for x in p})
                inert = {t for v in wit.values() for t in v["inert"]}
                c["inert"] = [t for t in members if t in inert]
            else:  # combo
                c["kept"] = [t for t in members if t in rel_ids]
            c["signatures"] = len({execute.signature(self.by_id[t]) for t in c["kept"]})
            kept.update(c["kept"])
            out.append(c)
        for name in sorted(self.context):
            if ("option", name) not in wit:
                continue
            using = [rid for rid in self.by_id if self.side[rid] == "success" and name in self.parsed[rid]["options"]
                     and self.group[rid] != "misc"]
            reps, seen = [], set()
            for rid in sorted(using, key=lambda i: (not self.by_id[i]["source"].startswith("agent"), i)):
                sig = execute.signature(self.by_id[rid])
                if sig not in seen:
                    seen.add(sig)
                    reps.append(rid)
            reps = reps[:10]
            out.append({"id": "option:" + name, "kind": "option", "context": True, "members": using,
                        "witness": [], "dependent": [], "kept": reps, "inert": [], "signatures": len(seen)})
            kept.update(reps)
        present = {c["id"] for c in out}
        for (kind, name), w in wit.items():
            if kind == "option" and w.get("dependent") and "option:" + name not in present:
                dep = [{"test": t, "control": cc} for t, cc in w["dependent"]]
                kept_ids = sorted({x for d in dep for x in (d["test"], d["control"])})
                out.append({"id": "option:" + name, "kind": "option", "members": [], "witness": [],
                            "dependent": dep, "kept": kept_ids, "inert": [],
                            "signatures": len({execute.signature(self.by_id[t]) for t in kept_ids})})
                kept.update(kept_ids)
        dropped_sweeps = {r["id"] for r in self.tests if r.get("sweep") and r["id"] not in sweep_kept}
        templates = self.error_templates(exclude=dropped_sweeps)
        err_ids = [m for t in templates for m in t["members"]]
        if err_ids:
            out.append({"id": "errors", "kind": "error", "kept": err_ids, "templates": templates})
            kept.update(err_ids)
        kept.update(t for t in rel_ids if self.side[t] != "unstable")
        # tests linked to checklist items stay, whatever their option group
        feat = featmod.frozen(self.sub, self)
        for it in feat:
            it["tests"] = [t for t in it["tests"] if not (self.by_id[t].get("sweep") and t not in sweep_kept)]
        feat_ids = {t for it in feat for t in it["tests"] if self.side.get(t) != "unstable"}
        if feat_ids:
            kept.update(feat_ids)
        self.feature_snapshot = feat
        dropped = sorted(set(self.by_id) - kept)
        return out, sorted(kept), dropped, wit

    # ------------------------------------------------------------- report
    def report(self, rules_summary=None):
        lines = []
        ok, errs, warns = ifacemod.check(self.sub)
        lines.append("interface check: " + ("ok" if ok else "FAILED"))
        lines += ["  ERROR " + e for e in errs]
        lines += ["  WARNING " + w for w in warns]
        if rules_summary:
            lines.append("")
            lines += rules_summary
        rows, gaps = self.coverage()
        n_opt = sum(1 for r in rows if r[0] == "option")
        n_env = sum(1 for r in rows if r[0] == "env")
        t_opt = sum(1 for r in rows if r[0] == "option" and r[2].startswith("triggered"))
        t_env = sum(1 for r in rows if r[0] == "env" and r[2].startswith("triggered"))
        lines.append("")
        lines.append(f"tests: {len(self.tests)}  options triggered {t_opt}/{n_opt}, env {t_env}/{n_env}")
        dep = [r for r in rows if r[2].startswith("triggered only with") or r[2].startswith("triggered (context")]
        for kind, name, status, n in dep:
            lines.append(f"  {name:<28} {status} ({n} test(s))")
        if gaps:
            lines.append("gaps:")
            for kind, name, why in gaps:
                lines.append(f"  {name if kind == 'option' else 'env ' + name:<28} {why}")
        else:
            lines.append("gaps: none")
        rel_errs = self.relation_errors()
        rel = self.relations()
        if rel_errs and rel_errs[0].startswith("relations.json missing"):
            lines.append("relations.json: not written yet")
        else:
            c = Counter(r.get("class") for r in rel if isinstance(r, dict))
            lines.append(f"relations.json: {len(rel)} entries (I {c.get('I', 0)}, II {c.get('II', 0)}, "
                         f"IV {c.get('IV', 0)})"
                         + ("" if not rel_errs else "  PROBLEMS: " + "; ".join(rel_errs)))
        sides = Counter(self.side.values())
        lines.append(f"success {sides.get('success', 0)}, error {sides.get('error', 0)}, "
                     f"unstable {sides.get('unstable', 0)}")
        unstable = [r for r in self.tests if self.side[r["id"]] == "unstable"]
        if unstable:
            lines.append("unstable tests (nondeterministic output, timeout or size cap; excluded from the SPEC "
                         "and the differential suite):")
            for r in unstable[:10]:
                why = "timeout" if r.get("timeout") else "size cap" if r.get("size_capped") else "two runs differed"
                lines.append(f"  {r['id']} exit {r['exit']} ({why}): {' '.join(r['argv'])[:60]}  {r.get('note', '')[:50]}")
            if len(unstable) > 10:
                lines.append(f"  ... {len(unstable) - 10} more")
        return "\n".join(lines)


def freeze(sub, analysis):
    clusters, kept, dropped, wit = analysis.clusters()
    rows, gaps = analysis.coverage(wit)
    doc = {"sub": sub, "clusters": clusters, "kept": kept, "dropped": dropped,
           "coverage": {"rows": rows, "gaps": gaps},
           "relations": analysis.relations(),
           "features": getattr(analysis, "feature_snapshot", []),
           "sweeps": read_json(sub_dir(sub, "sweeps.json"), [])}
    write_json(sub_dir(sub, "clusters.json"), doc)
    write_json(sub_dir(sub, "suite.json"), {"sub": sub, "tests": kept})
    return doc
