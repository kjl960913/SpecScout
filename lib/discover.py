"""Step 1 script: help tree, help bytes, documentation copy."""
import os
import re
import shutil

import execute
from common import read_json, slug, write_json, wpath

SECTION = re.compile(r"^\s*(available\s+)?(sub-?)?commands?\s*:?\s*$|^\s*SUBCOMMANDS:?\s*$|^\s*COMMANDS:?\s*$", re.I)
ENTRY = re.compile(r"^\s{1,8}([A-Za-z][\w-]*)(?:,\s*[\w-]+)*(?:\s{2,}|\t|\s*$)")
HELP_WORDS = {"help"}
DOC_MAX = 256 * 1024


def run(binary, argv):
    blobs = execute.Blobs()
    rec = execute.run_once(binary, {"argv": argv, "channel": "pipe"}, blobs)
    out = blobs.get(rec["stdout_sha"]) or b""
    err = blobs.get(rec["stderr_sha"]) or b""
    return rec["exit"], out, err


def capture_help(binary, path):
    tries = [path + ["--help"], path + ["-h"]]
    if path:
        tries += [["help"] + path, path + ["help"]]
    best = None
    for argv in tries:
        rc, out, err = run(binary, argv)
        text = out or err
        score = (rc == 0, bool(out), len(text))
        if best is None or score > best[0]:
            best = (score, argv, rc, text)
        if rc == 0 and out:
            break
    _, argv, rc, text = best
    return argv, rc, text


def candidates(text):
    lines = text.decode("utf-8", "replace").splitlines()
    out, inside = [], False
    for line in lines:
        if SECTION.match(line):
            inside = True
            continue
        if inside:
            if not line.strip():
                # a blank line ends the section only when the next block is not indented
                continue
            if not line[:1].isspace():
                inside = False
                continue
            m = ENTRY.match(line)
            if m and m.group(1) not in out:
                out.append(m.group(1))
    return out


def add_node(binary, path, nodes, rejected=None, parent_help=None):
    argv, rc, text = capture_help(binary, path)
    if parent_help is not None and (rc != 0 or text == parent_help or not text):
        if rejected is not None:
            rejected.append({"path": path, "exit": rc, "reason": "help not distinct or failed"})
        return None
    s = slug(path)
    rel = os.path.join("help", s + ".txt")
    os.makedirs(wpath("help"), exist_ok=True)
    with open(wpath(rel), "wb") as f:
        f.write(text)
    n = {"path": path, "slug": s, "help_file": rel, "help_argv": argv, "help_exit": rc}
    nodes.append(n)
    return n, text


def walk(binary, maxdepth=3):
    nodes, rejected = [], []
    root = add_node(binary, [], nodes)
    queue = [(root[0], root[1])]
    while queue:
        n, text = queue.pop(0)
        if len(n["path"]) >= maxdepth:
            continue
        for c in candidates(text):
            if c in HELP_WORDS:
                continue
            path = n["path"] + [c]
            if any(x["path"] == path for x in nodes):
                continue
            got = add_node(binary, path, nodes, rejected, parent_help=text)
            if got:
                queue.append(got)
    return nodes, rejected


def copy_docs(root, binary):
    dest = wpath("docs")
    copied = []
    binary = os.path.realpath(binary)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in (".git",) and not d.startswith("."))
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.realpath(full) == binary or re.match(r"(?i)^(license|copying)", name):
                continue
            try:
                if os.path.islink(full) or os.path.getsize(full) > DOC_MAX:
                    continue
                with open(full, "rb") as f:
                    head = f.read(8192)
            except OSError:
                continue
            if b"\0" in head:
                continue
            rel = os.path.relpath(full, root)
            os.makedirs(os.path.dirname(os.path.join(dest, rel)), exist_ok=True)
            shutil.copyfile(full, os.path.join(dest, rel))
            copied.append(rel)
    return copied


def discover(binary, docs_root=None, maxdepth=3):
    binary = os.path.abspath(binary)
    os.makedirs(wpath("bin"), exist_ok=True)
    link = wpath("bin", "executable")
    if os.path.lexists(link):
        os.unlink(link)
    os.symlink(binary, link)
    nodes, rejected = walk(binary, maxdepth)
    docs = copy_docs(docs_root or os.path.dirname(binary), binary)
    write_json(wpath("subcommands.json"), {"binary": binary, "argv0": execute.ARGV0, "nodes": nodes,
                                           "rejected_candidates": rejected})
    write_json(wpath("docs_index.draft.json"),
               {"docs/" + d: {"size": os.path.getsize(wpath("docs", d)), "commands": [], "summary": ""}
                for d in docs})
    return nodes, docs


def binary_path():
    doc = read_json(wpath("subcommands.json")) or {}
    return doc.get("binary") or wpath("bin", "executable")


def check():
    errs = []
    doc = read_json(wpath("subcommands.json"))
    if not doc or not doc.get("nodes"):
        return ["subcommands.json missing or has no nodes"]
    slugs = set()
    for n in doc["nodes"]:
        if n.get("slug") != slug(n.get("path", [])):
            errs.append(f"node {n.get('path')}: slug mismatch (use ./pk node add/remove)")
        if not os.path.exists(wpath(n.get("help_file", ""))):
            errs.append(f"node {n.get('slug')}: help file missing")
        slugs.add(n.get("slug"))
    if "root" not in slugs:
        errs.append("root node missing")
    docs = []
    if os.path.isdir(wpath("docs")):
        for dirpath, _, files in os.walk(wpath("docs")):
            docs += ["docs/" + os.path.relpath(os.path.join(dirpath, f), wpath("docs")) for f in files]
    index = read_json(wpath("docs_index.json"))
    if index is None:
        errs.append("docs_index.json missing")
    elif not isinstance(index, dict):
        errs.append("docs_index.json must map doc path -> {size, commands, summary}")
    else:
        for d in docs:
            if d not in index:
                errs.append(f"docs_index.json: {d} not listed")
        for d, v in index.items():
            if not isinstance(v, dict) or not isinstance(v.get("commands"), list):
                errs.append(f"docs_index.json: {d} needs a 'commands' list of command slugs")
            else:
                bad = [c for c in v["commands"] if c not in slugs]
                if bad:
                    errs.append(f"docs_index.json: {d} names unknown commands {bad}")
    return errs
