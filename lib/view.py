"""Compact textual views of test records (tool output and evidence files)."""
import shlex


def text(data, limit):
    if data is None:
        return "<missing>"
    s = data.decode("utf-8", "backslashreplace")
    if len(s) > limit:
        return s[:limit] + f"\n... [{len(s) - limit} more chars]"
    return s


def argv_str(argv):
    return "executable " + " ".join(shlex.quote(a) for a in argv) if argv else "executable"


def conditions(rec):
    parts = []
    files = rec.get("files") or {}
    if files:
        parts.append("files: " + ", ".join(f"{n}={v[:10] if not v.startswith('@') else v}"
                                           for n, v in sorted(files.items())))
    if rec.get("stdin"):
        parts.append("stdin: blob " + rec["stdin"]["sha"][:10])
    if rec.get("env"):
        parts.append("env: " + " ".join(f"{k}={v}" for k, v in sorted(rec["env"].items())))
    if rec.get("setup"):
        parts.append("setup: " + " ; ".join(argv_str(s) for s in rec["setup"]))
    parts.append("channel: " + rec.get("channel", "pipe"))
    if rec.get("tui"):
        t = rec["tui"]
        parts.append(f"keys: {' '.join(t.get('keys') or []) or '(none)'}  size {t.get('size', [80, 24])}"
                     + ("  [still running after the last key]" if rec.get("tui_running") else ""))
    return "   ".join(parts)


def flags(rec):
    out = []
    if rec.get("nondet"):
        out.append("NONDETERMINISTIC (two runs differed)")
    if rec.get("masked"):
        out.append("numbers differ between runs; compared with digits masked as <N>")
    if rec.get("timeout"):
        out.append("TIMEOUT")
    if rec.get("size_capped"):
        out.append("OUTPUT SIZE CAPPED")
    return out


def brief(rec, blobs, out_limit=2000, err_limit=1000):
    lines = [f"{rec['id']}  exit={rec['exit']}  {conditions(rec)}"]
    lines += ["  [" + f + "]" for f in flags(rec)]
    out = blobs.get(rec["stdout_sha"])
    err = blobs.get(rec["stderr_sha"])
    lines.append(f"--- stdout ({rec['stdout_len']} bytes)" + (" ---" if rec["stdout_len"] else ", empty ---"))
    if rec["stdout_len"]:
        lines.append(text(out, out_limit))
    lines.append(f"--- stderr ({rec['stderr_len']} bytes)" + (" ---" if rec["stderr_len"] else ", empty ---"))
    if rec["stderr_len"]:
        lines.append(text(err, err_limit))
    if rec.get("outfiles"):
        lines.append("--- files written/changed: " + ", ".join(
            f"{n} ({'blob ' + h[:10] if h and not h.startswith(('<', 'dir', 'link')) else h})"
            for n, h in sorted(rec["outfiles"].items())))
    if rec.get("statefiles"):
        lines.append("--- HOME state files: " + ", ".join(sorted(rec["statefiles"])))
    lines.append(f"(full bytes: work/blobs/<2 chars>/<sha>; stdout {rec['stdout_sha']})")
    return "\n".join(lines)


def evidence(rec, blobs, out_limit=1500, err_limit=800, title=None):
    """Markdown block for evidence files."""
    lines = [f"#### {title or rec['id']}  [{rec['source']}]" + (f"  note: {rec['note']}" if rec.get("note") else "")]
    lines.append("```")
    lines.append(argv_str(rec["argv"]))
    lines.append(conditions(rec))
    lines.append(f"exit {rec['exit']}" + "".join("  [" + f + "]" for f in flags(rec)))
    for name, limit in (("stdout", out_limit), ("stderr", err_limit)):
        n = rec[name + "_len"]
        if n:
            lines.append(f"--- {name} ({n} bytes)")
            lines.append(text(blobs.get(rec[name + "_sha"]), limit))
        else:
            lines.append(f"--- {name}: empty")
    for fname, h in sorted((rec.get("outfiles") or {}).items()):
        if h and len(h) == 40:
            lines.append(f"--- wrote {fname}")
            lines.append(text(blobs.get(h), 600))
        else:
            lines.append(f"--- {fname}: {h}")
    lines.append("```")
    return "\n".join(lines)


def fixtures(recs, blobs, limit=1200):
    """Contents of every distinct input file / stdin referenced by the records."""
    seen, lines = {}, []
    for r in recs:
        for name, ref in sorted((r.get("files") or {}).items()):
            if ref and not ref.startswith("@") and ref not in seen:
                seen[ref] = name
        if r.get("stdin") and r["stdin"].get("sha") not in seen:
            seen[r["stdin"]["sha"]] = "(stdin)"
    for sha, name in seen.items():
        data = blobs.get(sha)
        lines.append(f"#### input {name} = blob {sha[:10]} ({len(data or b'')} bytes)")
        lines.append("```")
        lines.append(text(data, limit))
        lines.append("```")
    return "\n".join(lines)
