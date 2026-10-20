"""The test store: work/<sub>/tests.jsonl, one record per executed test."""
import json
import os
from concurrent.futures import ProcessPoolExecutor

from common import append_jsonl, locked, read_json, read_jsonl, sub_dir, write_json, wpath
import execute

PREFIX = {"agent": "t", "rule": "r", "control": "c", "verify": "v", "impl": "i", "sweep": "s", "batch": "b"}
INPUT_FIELDS = ("argv", "files", "stdin", "env", "channel", "setup", "tui")


def tests_path(sub):
    return sub_dir(sub, "tests.jsonl")


def load(sub):
    return read_jsonl(tests_path(sub))


def by_id(sub):
    return {r["id"]: r for r in load(sub)}


def canonical(test):
    """Input half of a test, with defaults filled and the channel resolved."""
    t = {"argv": list(test["argv"]),
         "files": dict(sorted((test.get("files") or {}).items())),
         "stdin": test.get("stdin") or None,
         "env": dict(sorted((test.get("env") or {}).items())),
         "channel": execute.channel_name(test),
         "setup": [list(s) for s in test.get("setup") or []],
         "tui": test.get("tui") or None}
    return t


def input_key(test):
    t = canonical(test)
    return json.dumps([t[k] for k in INPUT_FIELDS], sort_keys=True)


def _allocate(sub, kind, n):
    path = sub_dir(sub, "ids.json")
    with locked(path + ".lock"):
        counters = read_json(path, {})
        start = counters.get(kind, 0)
        counters[kind] = start + n
        write_json(path, counters)
    return [f"{sub}-{PREFIX[kind]}{start + i + 1:04d}" for i in range(n)]


def _kind(source):
    head = source.split(":", 1)[0]
    if source == "rule:control":
        return "control"
    return {"agent": "agent", "rule": "rule", "verify": "verify", "impl": "impl", "sweep": "sweep",
            "batch": "batch"}[head]


def _run(args):
    binary, test = args
    return execute.run_test(binary, test, execute.Blobs())


def default_jobs():
    return max(1, min(8, os.cpu_count() or 1))


def run_and_record(sub, binary, items, jobs=None, path=None):
    """items: [(test, source, note, extra)] -> records appended to the store, in order."""
    if not items:
        return []
    tests = [canonical(t) for t, _, _, _ in items]
    jobs = jobs or default_jobs()
    if len(tests) == 1 or jobs == 1:
        obs = [_run((binary, t)) for t in tests]
    else:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            obs = list(pool.map(_run, [(binary, t) for t in tests], chunksize=4))
    out = []
    by_kind = {}
    for i, (_, source, _, _) in enumerate(items):
        by_kind.setdefault(_kind(source), []).append(i)
    ids = [None] * len(items)
    for kind, idx in by_kind.items():
        for i, ident in zip(idx, _allocate(sub, kind, len(idx))):
            ids[i] = ident
    for i, (t, (_, source, note, extra)) in enumerate(zip(tests, items)):
        rec = {"id": ids[i], "sub": sub, **t, **obs[i], "source": source, "note": note or ""}
        rec.update(extra or {})
        append_jsonl(path or tests_path(sub), rec)
        out.append(rec)
    return out
