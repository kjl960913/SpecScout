"""`./pk sweep` and `./pk batch`: run one test template once per word / candidate and summarise the reactions.

sweep: the agent gives the template (where {W} goes: argv tokens, --stdin-text, --env values, fixture file
contents and file names); the word lists are generic (templates/wordlists/*.txt) or the agent's own.
Every word test is recorded with source "sweep:<covers id>" and rec["sweep"] = {"run", "word"}.

batch: the same mechanism for guessing how something is written. The candidates are whole spellings
(a statement, an expression, a line or several lines of a program, an argument list with --split) and
the placeholder is {C}. Batch runs are numbered B01, B02 ... and their tests carry
rec["sweep"] = {"run", "word", "batch": true}, so clustering, filtering (all accepted candidates, two
representatives of the majority rejection) and the SPEC treat them like sweep runs. A batch covers a
`construct` item: an accepted candidate is a successful linked test, failed candidates count as failed
attempts for "unreachable".
"""
import json
import os
import re
import shlex
from collections import Counter, OrderedDict

import execute
import testdb
from common import FRAMEWORK, read_json, sub_dir, write_json

MAX_WORDS = 600
MAX_CANDIDATES = 200
WORDLISTS = os.path.join(FRAMEWORK, "templates", "wordlists")
PLACEHOLDER = "{W}"
CAND = "{C}"
SEPARATOR = "---"


def list_names():
    try:
        return sorted(f[:-4] for f in os.listdir(WORDLISTS) if f.endswith(".txt"))
    except OSError:
        return []


# vocab item id word -> framework list that a sweep of that item always includes
AUTO_LISTS = (("symbol", "symbol_names"), ("class", "symbol_names"), ("categor", "unicode_categories"),
              ("function", "math_functions"), ("repl", "repl_keywords"), ("command", "repl_keywords"),
              ("keyword", "lang_keywords"), ("file", "filenames"))


def auto_lists(covers):
    """Framework lists matching a vocab item id (vocab:symbol -> symbol_names ...)."""
    name = covers.split(":", 1)[-1].lower()
    out = []
    for key, lst in AUTO_LISTS:
        if key in name and lst not in out and lst in list_names():
            out.append(lst)
    return out


def read_words(names, files):
    words = []
    for n in names or []:
        p = os.path.join(WORDLISTS, n + ".txt")
        if not os.path.exists(p):
            raise SystemExit(f"ERROR: no word list {n!r}; available: {', '.join(list_names())}")
        words += _read(p)
    for f in files or []:
        words += _read(f)
    out = list(OrderedDict.fromkeys(w for w in words if w))
    if not out:
        raise SystemExit("ERROR: the word list is empty")
    if len(out) > MAX_WORDS:
        raise SystemExit(f"ERROR: {len(out)} words; at most {MAX_WORDS} per sweep")
    return out


def _read(path):
    with open(path, encoding="utf-8") as f:
        return [l.rstrip("\n") for l in f if l.strip() and not l.startswith("#")]


def read_candidates(files, inline):
    """Candidates from files (one per line, every nonblank line counts; with a line consisting of ---
    anywhere in the file, the blocks between such lines instead, so a candidate may span several lines)
    and from --cand."""
    cands = []
    for path in files or []:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        lines = text.split("\n")
        if any(l.strip() == SEPARATOR for l in lines):
            block = []
            for l in lines + [SEPARATOR]:
                if l.strip() == SEPARATOR:
                    c = "\n".join(block).strip("\n")
                    if c.strip():
                        cands.append(c + ("\n" if "\n" in c else ""))
                    block = []
                else:
                    block.append(l)
        else:
            cands += [l for l in lines if l.strip()]
    cands += [c for c in inline or [] if c != ""]
    out = list(OrderedDict.fromkeys(cands))
    if not out:
        raise SystemExit("ERROR: no candidates (give --cands FILE and/or --cand TEXT)")
    if len(out) > MAX_CANDIDATES:
        raise SystemExit(f"ERROR: {len(out)} candidates; at most {MAX_CANDIDATES} per batch")
    return out


def instantiate(template, word, blobs, raw_files, placeholder=PLACEHOLDER, split=False):
    """template: canonical test with the placeholder in strings; raw_files: {name: bytes|'@dir'} before
    blob storage. split: an argument that is exactly the placeholder becomes the shell-split candidate."""
    w = word
    t = json.loads(json.dumps(template))
    argv = []
    for a in t["argv"]:
        if split and a == placeholder:
            argv += shlex.split(w)
        else:
            argv.append(a.replace(placeholder, w))
    t["argv"] = argv
    t["env"] = {k: (v.replace(placeholder, w) if isinstance(v, str) else v) for k, v in (t.get("env") or {}).items()}
    files = {}
    for name, data in raw_files.items():
        nname = name.replace(placeholder, w)
        if data == "@dir":
            files[nname] = "@dir"
        else:
            files[nname] = blobs.put(data.replace(placeholder.encode(), w.encode("utf-8")))
    t["files"] = files
    if template.get("_stdin_raw") is not None:
        t["stdin"] = {"sha": blobs.put(template["_stdin_raw"].replace(placeholder.encode(), w.encode("utf-8")))}
    t.pop("_stdin_raw", None)
    t["setup"] = [[a.replace(placeholder, w) for a in s] for s in t.get("setup") or []]
    return t


def reaction(rec, word, blobs):
    """What the program did with a word, with the word itself replaced by {W}."""
    def norm(data):
        data = data or b""
        if word:
            data = data.replace(word.encode("utf-8"), b"{W}")
        if rec.get("masked"):
            data = execute.mask_numbers(data)
        return data
    out = norm(blobs.get(rec.get("stdout_sha")))
    err = norm(blobs.get(rec.get("stderr_sha")))
    return (rec.get("exit"), out, err, bool(rec.get("timeout")), bool(rec.get("nondet")))


def classify(recs, blobs, nonerror=()):
    """Group word tests by reaction. Returns (groups, rejected_key) where groups is
    [{"key", "words", "ids", "success"}] largest first and rejected_key the majority failing reaction."""
    groups = OrderedDict()
    for r in recs:
        w = r["sweep"]["word"]
        k = reaction(r, w, blobs)
        g = groups.setdefault(k, {"key": k, "words": [], "ids": [],
                                  "success": execute.stable(r) and (r["exit"] == 0 or r["exit"] in nonerror)})
        g["words"].append(w)
        g["ids"].append(r["id"])
    ordered = sorted(groups.values(), key=lambda g: -len(g["words"]))
    failing = [g for g in ordered if not g["success"]]
    rejected = failing[0]["key"] if failing else None
    return ordered, rejected


def _first_line(data):
    s = data.decode("utf-8", "replace").strip()
    return (s.splitlines() or [""])[0][:110]


def show(word):
    """One-line display of a word or candidate."""
    w = word.rstrip("\n").replace("\n", " ⏎ ")
    return w if len(w) <= 60 else w[:57] + "..."


def summary(sub, run_id, recs, blobs, nonerror=(), batch=False):
    groups, rejected = classify(recs, blobs, nonerror)
    total = len(recs)
    acc = [w for g in groups if g["success"] for w in g["words"]]
    noun = "candidates" if batch else "words"
    one = "candidate" if batch else "word"
    lines = [f"{'batch' if batch else 'sweep'} {run_id}: {total} {noun}, {len(acc)} accepted (successful), "
             f"{total - len(acc)} not"]
    succ = [g for g in groups if g["success"]]
    if succ:
        lines.append(f"  ACCEPTED ({len(acc)}), {one} -> first line of output:")
        shown = 0
        for g in succ:
            exit_, out, err, _, _ = g["key"]
            first = _first_line(out) or (("stderr: " + _first_line(err)) if err.strip() else "(no output)")
            for w, rid in zip(g["words"], g["ids"]):
                if shown < 80:
                    lines.append(f"    {show(w):<24} -> {first}   ({rid})")
                shown += 1
        if shown > 80:
            lines.append(f"    ... {shown - 80} more (./pk tests {sub} --sweep {run_id})")
    fail = [g for g in groups if not g["success"]]
    for g in fail[:10]:
        exit_, out, err, to, nd = g["key"]
        what = "REJECTED (majority)" if g["key"] == rejected else "failed differently"
        msg = _first_line(err) or _first_line(out) or "(no output)"
        sep = " | " if batch else " "
        words = sep.join(show(w) for w in g["words"][:30]) + (f" ... (+{len(g['words']) - 30})" if len(g["words"]) > 30 else "")
        lines.append(f"  {len(g['words']):>4}  exit {exit_}{' TIMEOUT' if to else ''}{' NONDET' if nd else ''}  "
                     f"{what}: {msg}")
        lines.append(f"        {noun}: {words}   e.g. {g['ids'][0]}")
    if len(fail) > 10:
        lines.append(f"  ... {len(fail) - 10} more failing reaction groups")
    if batch:
        if not acc:
            lines.append("  none accepted: read the messages above (a parser usually says what it expected at which "
                         "position), write the next batch of spellings from them, from the documentation examples "
                         "and from similar languages/tools you know.")
        return lines, acc
    if len(succ) == 1 and len(succ[0]["words"]) == total and total > 3:
        lines.append("  WARNING: every word behaved identically (output equal up to the word itself): the template "
                     "probably treats the word as plain data and does not exercise the vocabulary.")
    if not acc:
        lines.append("  WARNING: no word was accepted: check the template (does a known-good name succeed in it?).")
    return lines, acc


MAX_BATCH_REJECTED = 6


def kept_ids(recs, blobs, nonerror=()):
    """Word tests that stay in the SPEC / differential suite: every accepted word, every word that failed
    differently from the majority (at most 3 per reaction), and 2 representatives of the majority rejection.
    Batch runs (guessed spellings, whose error messages usually quote the candidate and so differ) keep
    every accepted candidate and one representative per rejection, at most MAX_BATCH_REJECTED in all."""
    groups, rejected = classify(recs, blobs, nonerror)
    batch = bool(recs and recs[0]["sweep"].get("batch"))
    keep, nrej = [], 0
    for g in groups:
        if g["success"]:
            keep += g["ids"]
        elif batch:
            if nrej < MAX_BATCH_REJECTED:
                keep += g["ids"][:1]
                nrej += 1
        elif g["key"] == rejected:
            keep += g["ids"][:2]
        else:
            keep += g["ids"][:3]
    return keep


def runs(tests):
    """{run id: [word records]} over a test list."""
    out = OrderedDict()
    for r in tests:
        s = r.get("sweep")
        if s:
            out.setdefault(s["run"], []).append(r)
    return out


def next_run_id(sub, prefix="S"):
    path = sub_dir(sub, "sweeps.json")
    doc = read_json(path, [])
    n = sum(1 for e in doc if str(e.get("run", "")).startswith(prefix)) + 1
    return f"{prefix}{n:02d}", doc


def record_run(sub, entry):
    path = sub_dir(sub, "sweeps.json")
    doc = read_json(path, [])
    doc.append(entry)
    write_json(path, doc)
