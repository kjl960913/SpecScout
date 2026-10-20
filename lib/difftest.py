"""Step 4: build the candidate and compare it with frozen gold observations."""
import difflib
import json
import os
import shutil
import subprocess
import time
from collections import Counter, OrderedDict
from concurrent.futures import ProcessPoolExecutor

import execute
import testdb
from common import nodes, read_json, read_jsonl, sub_dir, write_json, write_text, wpath

CAND_TIMEOUT = 10.0
FIELDS = ("exit", "stdout_sha", "stderr_sha", "outfiles")


def suite():
    """[(sub, test record, [entries])] over every command with a built SPEC."""
    out = []
    for n in nodes():
        s = read_json(sub_dir(n["slug"], "suite.json"))
        if not s:
            continue
        tests = testdb.by_id(n["slug"])
        index = read_json(sub_dir(n["slug"], "entry_index.json"), {})
        for t in s.get("tests", []) + s.get("verify", []):
            if t in tests and execute.stable(tests[t]):
                out.append((n["slug"], tests[t], index.get(t, ["unmapped"])))
    return out


def build():
    src = wpath("impl")
    if not os.path.isfile(os.path.join(src, "compile.sh")):
        return None, "work/impl/compile.sh not found"
    dest = wpath("diff", "build")
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(src, dest, symlinks=True,
                    ignore=shutil.ignore_patterns("executable", "__pycache__", "*.pyc", ".git"))
    t0 = time.time()
    try:
        p = subprocess.run(["bash", "compile.sh"], cwd=dest, capture_output=True, text=True, timeout=1800)
        log = p.stdout + p.stderr
        rc = p.returncode
    except subprocess.TimeoutExpired as e:
        return None, f"compile.sh timed out after 1800s\n{e.stdout or ''}{e.stderr or ''}"
    exe = os.path.join(dest, "executable")
    if rc != 0 or not os.path.isfile(exe) or not os.access(exe, os.X_OK):
        return None, f"compile.sh exit {rc}; executable {'missing' if not os.path.exists(exe) else 'not executable'}\n" + log[-4000:]
    return exe, f"built in {time.time() - t0:.1f}s"


def _compare(args):
    exe, rec = args
    blobs = execute.Blobs()
    test = {k: rec.get(k) for k in testdb.INPUT_FIELDS}
    cand = execute.run_once(exe, test, blobs, timeout=CAND_TIMEOUT)
    if rec.get("masked"):
        # the original's own runs differed only in numbers: compare its streams with digits masked
        diffs = [f for f in ("exit", "outfiles") if cand.get(f) != rec.get(f)]
        diffs += [s + "_sha" for s in ("stdout", "stderr") if cand.get(s + "_msha") != rec.get(s + "_msha")]
    else:
        diffs = [f for f in FIELDS if cand.get(f) != rec.get(f)]
    if cand.get("timeout"):
        diffs = sorted(set(diffs) | {"timeout"})
    return {"id": rec["id"], "pass": not diffs, "diffs": diffs,
            "cand": {f: cand.get(f) for f in FIELDS}, "cand_timeout": cand.get("timeout")}


def run(jobs=None):
    exe, log = build()
    runs = sorted(d for d in os.listdir(wpath("diff")) if d.startswith("run")) if os.path.isdir(wpath("diff")) else []
    name = f"run{len(runs) + 1:03d}"
    rdir = wpath("diff", name)
    os.makedirs(rdir, exist_ok=True)
    if exe is None:
        write_text(os.path.join(rdir, "build.log"), log)
        return None, "BUILD FAILED\n" + log
    items = suite()
    jobs = jobs or testdb.default_jobs()
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(_compare, [(exe, rec) for _, rec, _ in items], chunksize=8))
    rows = []
    for (sub, rec, entries), res in zip(items, results):
        rows.append({**res, "sub": sub, "entries": [f"{sub}:{e}" for e in entries]})
    with open(os.path.join(rdir, "results.jsonl"), "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    prev = read_jsonl(wpath("diff", runs[-1], "results.jsonl")) if runs else []
    text = summary(rows, prev, log)
    write_text(os.path.join(rdir, "summary.txt"), text)
    return rows, text


def summary(rows, prev, log):
    n = len(rows)
    ok = sum(r["pass"] for r in rows)
    lines = [f"{log.splitlines()[0] if log else 'built'}   pass {ok}/{n} ({100.0 * ok / max(n, 1):.1f}%)"]
    if prev:
        before = {r["id"]: r["pass"] for r in prev}
        fixed = [r["id"] for r in rows if r["pass"] and before.get(r["id"]) is False]
        broke = [r["id"] for r in rows if not r["pass"] and before.get(r["id"]) is True]
        lines.append(f"vs previous run: +{len(fixed)} fixed, -{len(broke)} regressions"
                     + (f" ({', '.join(broke[:8])}{' ...' if len(broke) > 8 else ''})" if broke else ""))
    per = OrderedDict()
    for r in rows:
        for e in r["entries"]:
            per.setdefault(e, []).append(r)
    bad = [(e, rs) for e, rs in per.items() if any(not r["pass"] for r in rs)]
    bad.sort(key=lambda x: -sum(not r["pass"] for r in x[1]))
    if bad:
        lines.append("failing SPEC entries (fail/total, first failing test and what differs):")
    for e, rs in bad[:40]:
        f = [r for r in rs if not r["pass"]]
        lines.append(f"  {e:<40} {len(f)}/{len(rs)}   e.g. {f[0]['id']} differs in {', '.join(f[0]['diffs'])}")
    if len(bad) > 40:
        lines.append(f"  ... {len(bad) - 40} more entries")
    lines.append("details: ./pk diff --entry <entry>   or   ./pk diff --test <test id>")
    return "\n".join(lines)


def latest():
    runs = sorted(d for d in os.listdir(wpath("diff")) if d.startswith("run")) if os.path.isdir(wpath("diff")) else []
    for r in reversed(runs):
        rows = read_jsonl(wpath("diff", r, "results.jsonl"))
        if rows:
            return rows
    return []


def _stream_diff(gold, cand, label, limit=40):
    g = (gold or b"").decode("utf-8", "backslashreplace").splitlines(keepends=True)
    c = (cand or b"").decode("utf-8", "backslashreplace").splitlines(keepends=True)
    d = list(difflib.unified_diff(g, c, f"gold {label}", f"yours {label}", n=2))
    if len(d) > limit:
        d = d[:limit] + [f"... {len(d) - limit} more diff lines\n"]
    out = "".join(x if x.endswith("\n") else x + "\\ (no newline)\n" for x in d)
    return out or f"{label}: identical text, bytes differ (encoding or trailing newline)\n"


def detail(row, blobs):
    import view
    sub = row["sub"]
    rec = testdb.by_id(sub)[row["id"]]
    lines = [f"{row['id']}  {view.argv_str(rec['argv'])}", "  " + view.conditions(rec),
             f"  differs in: {', '.join(row['diffs'])}"]
    if "exit" in row["diffs"]:
        lines.append(f"  exit: gold {rec['exit']}  yours {row['cand']['exit']}")
    for s in ("stdout", "stderr"):
        if s + "_sha" in row["diffs"]:
            lines.append(_stream_diff(blobs.get(rec[s + "_sha"]), blobs.get(row["cand"][s + "_sha"]), s))
    if "outfiles" in row["diffs"]:
        lines.append(f"  files: gold {json.dumps(rec.get('outfiles'))}\n         yours {json.dumps(row['cand']['outfiles'])}")
    if row.get("cand_timeout"):
        lines.append(f"  your program timed out after {CAND_TIMEOUT}s")
    return "\n".join(lines)
