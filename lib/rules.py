"""Step 2.6: rule-generated parser/boundary tests around an automatically chosen base."""
import json
import os
from collections import Counter

import argvparse
import errtemplate
import testdb
from common import read_json, sub_dir, write_json

TERMINAL = ("tty", "tty_out", "stdin_tty", "tui")
SCALAR_VALUES = ["0", "-1", "1.5", "99999999999999999999", "abc", " ", "\t", "%s%n", "../x", "*",
                 "é字", "a" * 300]
HELPISH = {"-h", "--help", "-V", "--version"}


def _ctxsig(rec, parsed):
    return json.dumps([rec.get("files") or {}, rec.get("stdin"), parsed["positionals"]], sort_keys=True)


def select_base(an, wit):
    declared = an.iface.get("positionals") or []
    want = sum(1 for p in declared if not p.get("repeated")) + sum(1 for p in declared if p.get("repeated"))
    controls = Counter()
    for subj in wit.values():
        for t, c in subj["witness"]:
            controls[_ctxsig(an.by_id[c], an.parsed[c])] += 1
    cands = []
    for order, r in enumerate(an.tests):
        p = an.parsed[r["id"]]
        if (not r["source"].startswith("agent:") or an.side[r["id"]] != "success" or r["exit"] != 0
                or not p["parse_ok"] or p["unknown"] or p["missing_value"] or r["channel"] in TERMINAL
                or r.get("env")):
            continue
        has_output = r["stdout_len"] > 0 or bool(r.get("outfiles"))
        n_inputs = len(r.get("files") or {}) + (1 if r.get("stdin") else 0)
        n_opts = sum(c for name, c in p["options"].items() if name not in an.context)
        key = (n_opts, 0 if has_output else 1, -controls[_ctxsig(r, p)],
               -min(n_inputs, 1), abs(len(p["positionals"]) - want), order)
        cands.append((key, r["id"]))
    if not cands:
        return None
    return min(cands)[1]


def representative_values(an, wit):
    reps = {}
    for o in an.iface.get("options", []):
        if not o.get("takes_value"):
            continue
        name = o["name"]
        vals = Counter()
        for t, _ in wit.get(("option", name), {}).get("witness", []):
            vals.update(an.parsed[t]["values"].get(name, []))
        if not vals:
            for rid, p in an.parsed.items():
                if an.side[rid] == "success":
                    vals.update(p["values"].get(name, []))
        reps[name] = vals.most_common(1)[0][0] if vals else "x"
    return reps


def _opt_tokens(o, value, spelling=None, form="separate"):
    s = spelling or o["name"]
    if not o.get("takes_value") or value is None:
        return [s]
    if form == "equals":
        return [f"{s}={value}"]
    if form == "attached":
        return [s + value]
    if o.get("takes_value") == "optional":
        return [f"{s}={value}"] if s.startswith("--") else [s + value]
    return [s, value]


def generate(an, base_id, reps, blobs):
    path = an.path
    out = []

    def add(phase, argv, note, files=None, stdin="base", channel=None):
        out.append((phase, argv, note, files, stdin, channel))

    b = an.by_id[base_id] if base_id else None
    options = an.iface.get("options", [])
    if not path:
        for argv, note in [([], "bare invocation"), (["--help"], "global help"), (["-h"], "global help short"),
                           (["--version"], "version"), (["-V"], "version short"),
                           (["nosuchcmd"], "unknown subcommand"),
                           (["nosuchcmd", "--help"], "unknown subcommand + help"),
                           (["--"], "bare separator"), (["--nosuchflag"], "unknown long flag"),
                           (["-Q"], "unknown short flag")]:
            add("1.global", argv, note, stdin=None, channel="pipe")
    else:
        head, last = path[:-1], path[-1]
        for v, note in [(last.upper(), "uppercase"), (last.capitalize(), "capitalized"),
                        (last[:-1], "truncated prefix"), (last + "x", "trailing char"),
                        (last + last, "doubled name")]:
            if v and v != last:
                add("1.sub", head + [v], f"command name {note}", stdin=None, channel="pipe")
        for extra, note in [(path[-1:], "repeated command word"), (["--"], "command + separator"),
                            (["--help"], "command help"), (["-h"], "command help short"),
                            (["nosuchflag"], "garbage token")]:
            add("1.sub", path + extra, note, stdin=None, channel="pipe")
        for o in options:
            add("1.nosub", head + _opt_tokens(o, reps.get(o["name"])), f"{o['name']} without the command word",
                stdin=None, channel="pipe")
    if b is None:
        return out
    p = an.parsed[base_id]
    opt_toks = [t for s in p["spans"] for t in b["argv"][s["start"]:s["end"]]]
    sep = ["--"] if p["separator"] else []
    pos = list(p["positionals"])
    files = dict(b.get("files") or {})

    def argv_of(extra_opts, positionals, after=None):
        return list(path) + extra_opts + opt_toks + sep + positionals + (after or [])

    n = len(pos)
    for k in range(0, n + 2):
        ps = pos[:k] if k <= n else pos + ["extra"]
        if k != n:
            add("2.arity", argv_of([], ps), f"{k} positional argument(s) (base has {n})")
    if n >= 2:
        for i in range(n):
            add("2.skip", argv_of([], pos[:i] + pos[i + 1:]), f"positional {i + 1} omitted, later ones shift left")
    for i in range(n):
        add("2.empty", argv_of([], pos[:i] + [""] + pos[i + 1:]), f"positional {i + 1} is an empty string")
    # Input files: every argv token naming a fixture file of the base -- a positional, or the value
    # of a context option such as -f FILE -- gets the path variants and the same-bytes-other-name variants.
    base_argv = list(b["argv"])
    slots = [i for i, tok in enumerate(base_argv) if i >= len(path) and tok in files and files[tok]
             and not files[tok].startswith("@")]

    def replaced(i, value):
        return base_argv[:i] + [value] + base_argv[i + 1:]

    for i in slots:
        tok, sha = base_argv[i], files[base_argv[i]]
        where = f"input file argument {tok!r}"
        for v, extra_files, stdin_ref, note in [
                ("-", {}, {"sha": sha}, "'-' with the same content on stdin"),
                ("nonexistent.txt", {}, "base", "path that does not exist"),
                ("pk_dir", {"pk_dir": "@dir"}, "base", "path is a directory"),
                ("pk_noperm", {"pk_noperm": "@noperm:" + sha}, "base", "file without read permission"),
                ("pk_dangling", {"pk_dangling": "@link:__nonexistent__"}, "base", "dangling symlink"),
                ("", {}, "base", "empty string")]:
            add("2.value", replaced(i, v), f"{where}: {note}", files={**files, **extra_files}, stdin=stdin_ref)
        stem, ext = os.path.splitext(tok)
        for new in sorted({stem + ".txt", stem, stem + ext.upper(), "renamed" + ext} - {tok}):
            if not new:
                continue
            nf = {k2: v2 for k2, v2 in files.items() if k2 != tok}
            nf[new] = sha
            add("2.name", replaced(i, new), f"{where}: same bytes named {new!r}", files=nf)
    for i, tok in enumerate(pos):
        if tok in files:
            continue
        for v in SCALAR_VALUES:
            add("2.value", argv_of([], pos[:i] + [v] + pos[i + 1:]), f"positional {i + 1} = {v[:20]!r}")
    # byte boundaries of the input
    target = None
    if b.get("stdin") and b["stdin"].get("sha"):
        target = ("stdin", None, b["stdin"]["sha"])
    elif slots:
        target = ("file", base_argv[slots[0]], files[base_argv[slots[0]]])
    if target:
        data = blobs.get(target[2]) or b""
        variants = [(b"", "empty input"), (b"\n", "a single newline")]
        if data.endswith(b"\n"):
            variants.append((data.rstrip(b"\n"), "no trailing newline"))
        if b"\n" in data:
            variants.append((data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"), "CRLF line endings"))
        variants.append((data + b"\xff\xfe\n", "invalid UTF-8 appended"))
        variants.append((b"\xef\xbb\xbf" + data, "UTF-8 BOM prefix"))
        for content, note in variants:
            sha = blobs.put(content)
            if target[0] == "stdin":
                add("2.bytes", base_argv, "stdin: " + note, stdin={"sha": sha})
            else:
                add("2.bytes", base_argv, f"{target[1]}: " + note, files={**files, target[1]: sha})
    # option spellings
    flags = [o for o in options if not o.get("takes_value") and o["name"] not in HELPISH
             and not set(o.get("aliases") or []) & HELPISH]
    for o in options:
        v = reps.get(o["name"])
        name = o["name"]
        variants = []
        for alias in o.get("aliases") or []:
            variants.append((_opt_tokens(o, v, alias), f"alias {alias}"))
        if o.get("takes_value") == "required":
            for s in [name] + list(o.get("aliases") or []):
                if s.startswith("--"):
                    variants.append((_opt_tokens(o, v, s, "equals"), f"{s}=VALUE form"))
                else:
                    variants.append((_opt_tokens(o, v, s, "attached"), f"{s}VALUE attached form"))
                    variants.append((_opt_tokens(o, v, s, "equals"), f"{s}=VALUE form"))
        variants.append((_opt_tokens(o, v, name.upper()), "uppercase spelling"))
        variants.append((_opt_tokens(o, v) * 2, "given twice"))
        if name.startswith("--"):
            variants.append((_opt_tokens(o, v, "-" + name[2:]), "single dash"))
            if len(name) > 3:
                variants.append((_opt_tokens(o, v, name[:-1]), "truncated (unique prefix?)"))
            variants.append((_opt_tokens(o, v, name + "x"), "trailing character"))
            if not o.get("takes_value"):
                variants.append(([name + "=1"], "flag given a value"))
        for toks, note in variants:
            add("3.variant", argv_of(toks, pos), f"{name}: {note}")
    for o in options:
        if o.get("takes_value") != "required":
            continue
        other = next((f["name"] for f in flags if f["name"] != o["name"]), None)
        add("3.novalue", argv_of([o["name"]], pos), f"{o['name']} without value, swallowing the next token")
        add("3.novalue", argv_of([], pos, [o["name"]]), f"{o['name']} as the last token without value")
        if other:
            add("3.novalue", argv_of([o["name"], other], pos), f"{o['name']} followed by another flag {other}")
    return out


def run(an, binary, blobs, force=False):
    """Choose a base, generate and execute the rule tests once. Returns summary lines."""
    marker = sub_dir(an.sub, "rules_base.json")
    if os.path.exists(marker) and not force:
        return summary(an, read_json(marker))
    wit = an.witnesses()
    base_id = select_base(an, wit)
    reps = representative_values(an, wit)
    raw = generate(an, base_id, reps, blobs)
    known = {testdb.input_key(r) for r in an.tests}
    items, seen = [], set()
    b = an.by_id.get(base_id) if base_id else None
    for phase, argv, note, files, stdin, channel in raw:
        t = {"argv": argv,
             "files": files if files is not None else dict((b or {}).get("files") or {}),
             "stdin": (b or {}).get("stdin") if stdin == "base" else stdin,
             "env": {},
             "channel": channel or ((b or {}).get("channel") or "pipe"),
             "setup": (b or {}).get("setup") or []}
        if t["channel"] == "stdin" and not t["stdin"]:
            t["channel"] = "pipe"
        if t["stdin"] and t["channel"] == "pipe":
            t["channel"] = "stdin"
        k = testdb.input_key(t)
        if k in known or k in seen:
            continue
        seen.add(k)
        items.append((t, "rule:" + phase, note, None))
    recs = testdb.run_and_record(an.sub, binary, items)
    doc = {"base": base_id,
           "reason": None if base_id else "no successful, option-free, non-terminal agent test without env",
           "representative_values": reps, "tests": [r["id"] for r in recs]}
    write_json(marker, doc)
    an.reload()
    return summary(an, doc)


def summary(an, doc):
    ids = [t for t in doc.get("tests", []) if t in an.by_id]
    lines = []
    base = doc.get("base")
    if base:
        b = an.by_id.get(base, {})
        lines.append(f"rule tests: base {base} (argv {json.dumps(b.get('argv', []))})")
    else:
        lines.append(f"rule tests: no base ({doc.get('reason')}); only command-level rules were run")
    phases = Counter(an.by_id[t]["source"].split(":", 1)[1] for t in ids)
    lines.append(f"  {len(ids)} tests (" + ", ".join(f"{k} {v}" for k, v in sorted(phases.items())) + ")")
    sides = Counter(an.side[t] for t in ids)
    lines.append(f"  success {sides.get('success', 0)}, error {sides.get('error', 0)}, "
                 f"unstable {sides.get('unstable', 0)}")
    groups = Counter()
    example = {}
    for t in ids:
        if an.side[t] != "error":
            continue
        k = errtemplate.error_key(an.by_id[t], an.blobs)
        groups[k] += 1
        example.setdefault(k, t)
    if groups:
        lines.append(f"  distinct error messages ({len(groups)}):")
        for (exit_, stream, text), cnt in groups.most_common(15):
            one = text.strip().splitlines()[0] if text.strip() else "(empty)"
            lines.append(f"    exit {exit_} {stream} x{cnt}: {one[:110]}   e.g. {example[(exit_, stream, text)]}")
        if len(groups) > 15:
            lines.append(f"    ... {len(groups) - 15} more")
    succ = [t for t in ids if an.side[t] == "success"]
    if succ:
        lines.append("  succeeded: " + "; ".join(
            f"{t} {an.by_id[t]['source'].split(':', 1)[1]} ({an.by_id[t]['note'][:40]})" for t in succ[:12])
            + (f" ... {len(succ) - 12} more" if len(succ) > 12 else ""))
    return lines
