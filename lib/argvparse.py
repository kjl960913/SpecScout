"""argv -> canonical option set, values, positionals, using interface.json."""
import re

NUMBER = re.compile(r"^-\d+(\.\d+)?$")


def option_table(iface):
    """spelling -> option dict (name and every alias)."""
    table = {}
    for o in (iface or {}).get("options", []):
        for spelling in [o["name"]] + list(o.get("aliases") or []):
            table.setdefault(spelling, o)
    return table


def _parse_tokens(argv, start, table, out):
    i = start
    end_of_opts = False
    while i < len(argv):
        tok = argv[i]
        if end_of_opts or tok == "-" or not tok.startswith("-"):
            out["positionals"].append(tok)
            i += 1
            continue
        if tok == "--":
            end_of_opts = True
            out["separator"] = True
            i += 1
            continue
        if tok.startswith("--"):
            name, eq, val = tok.partition("=")
            o = table.get(name)
            if o is None:
                out["unknown"].append(tok)
                i += 1
                continue
            span = [i, i + 1]
            value = val if eq else None
            if not eq and o.get("takes_value") == "required":
                if i + 1 < len(argv) and (argv[i + 1] == "-" or not argv[i + 1].startswith("-")):
                    value = argv[i + 1]
                    span[1] = i + 2
                else:
                    out["missing_value"].append(o["name"])
            _add(out, o["name"], value, span)
            i = span[1]
            continue
        # short option(s)
        o = table.get(tok)
        if o is not None:
            span = [i, i + 1]
            value = None
            if o.get("takes_value") == "required":
                if i + 1 < len(argv) and (argv[i + 1] == "-" or not argv[i + 1].startswith("-")):
                    value = argv[i + 1]
                    span[1] = i + 2
                else:
                    out["missing_value"].append(o["name"])
            _add(out, o["name"], value, span)
            i = span[1]
            continue
        if "=" in tok and table.get(tok.split("=", 1)[0]) is not None:
            o = table[tok.split("=", 1)[0]]
            _add(out, o["name"], tok.split("=", 1)[1], [i, i + 1])
            i += 1
            continue
        chars = tok[1:]
        found = []
        j, ok, value, consumed_next = 0, True, None, False
        while j < len(chars):
            o = table.get("-" + chars[j])
            if o is None:
                ok = False
                break
            if o.get("takes_value") in ("required", "optional"):
                rest = chars[j + 1:]
                if rest:
                    value = rest[1:] if rest.startswith("=") else rest
                elif o.get("takes_value") == "required" and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                    value, consumed_next = argv[i + 1], True
                found.append((o["name"], value))
                break
            found.append((o["name"], None))
            j += 1
        if not ok or not found:
            if NUMBER.match(tok):
                out["positionals"].append(tok)
            else:
                out["unknown"].append(tok)
            i += 1
            continue
        span = [i, i + 2 if consumed_next else i + 1]
        for name, v in found:
            _add(out, name, v, span)
        i = span[1]


def _add(out, name, value, span):
    out["options"][name] = out["options"].get(name, 0) + 1
    if value is not None:
        out["values"].setdefault(name, []).append(value)
    out["spans"].append({"name": name, "start": span[0], "end": span[1]})


def parse(argv, sub_path, iface, root_iface=None):
    """Return {options:{name:count}, values, positionals, unknown, spans, parse_ok}."""
    out = {"options": {}, "values": {}, "positionals": [], "unknown": [], "spans": [],
           "missing_value": [], "separator": False, "parse_ok": True}
    sub_path = list(sub_path or [])
    globals_table = option_table(root_iface) if root_iface else {}
    table = dict(globals_table)
    table.update(option_table(iface))
    i = 0
    if sub_path:
        # leading global options, then the command words
        while i < len(argv) and argv[i].startswith("-") and argv[i] not in ("-", "--"):
            o = globals_table.get(argv[i].split("=", 1)[0])
            if o is None:
                out["parse_ok"] = False
                return out
            span = [i, i + 1]
            value = argv[i].split("=", 1)[1] if "=" in argv[i] else None
            if value is None and o.get("takes_value") == "required" and i + 1 < len(argv):
                value, span[1] = argv[i + 1], i + 2
            _add(out, o["name"], value, span)
            i = span[1]
        if argv[i:i + len(sub_path)] != sub_path:
            out["parse_ok"] = False
            return out
        i += len(sub_path)
    _parse_tokens(argv, i, table, out)
    return out


def option_set(parsed):
    return frozenset(parsed["options"])


def remove_option(argv, parsed, name):
    """argv with every occurrence of option `name` (and its value) removed.

    Returns None when the option shares a bundled token with another option."""
    drop = set()
    for s in parsed["spans"]:
        if s["name"] != name:
            continue
        others = [t for t in parsed["spans"] if t is not s and t["start"] == s["start"] and t["name"] != name]
        if others:
            return None
        drop.update(range(s["start"], s["end"]))
    return [t for k, t in enumerate(argv) if k not in drop]
