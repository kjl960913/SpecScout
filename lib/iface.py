"""interface.json: draft from help text, and validation."""
import os
import re

from common import read_json, sub_dir, wpath, node

SPELLING = re.compile(r"(?<![\w-])(--?[A-Za-z0-9?][\w.-]*)")
LONG_SPLIT = re.compile(r"\s{2,}|\t")
USAGE = re.compile(r"^\s*usage\s*:?\s*(.*)$", re.I)
GENERIC = {"OPTIONS", "OPTION", "FLAGS", "COMMAND", "COMMANDS", "SUBCOMMAND", "ARGS", "OPTIONS...", "OPTION..."}
ENV_CLAP = re.compile(r"\[env:\s*([A-Z_][A-Z0-9_]*)")
ENV_WORD = re.compile(r"\$?\b([A-Z][A-Z0-9]*_[A-Z0-9_]*[A-Z0-9])\b")


def help_text(sub):
    n = node(sub)
    path = wpath(n["help_file"])
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _option_from_line(line):
    head = LONG_SPLIT.split(line.strip(), 1)[0]
    spellings = SPELLING.findall(head)
    spellings = [s for s in spellings if s not in ("-", "--")]
    if not spellings:
        return None
    takes = False
    if "[=" in head or re.search(r"\[<[^>]+>\]|\[[A-Z][A-Z0-9_-]*\]", head):
        takes = "optional"
    elif re.search(r"=\S|<[^>]+>|\s[A-Z][A-Z0-9_-]+\b", head):
        takes = "required"
    longs = [s for s in spellings if s.startswith("--")]
    name = longs[0] if longs else spellings[0]
    aliases = []
    for s in spellings:
        s = s.rstrip(".,")
        if s != name and s not in aliases:
            aliases.append(s)
    o = {"name": name.rstrip(".,"), "aliases": aliases, "takes_value": takes, "source": "help"}
    if head.rstrip().endswith("..."):
        o["repeatable"] = True
    return o


def draft(sub):
    text = help_text(sub)
    n = node(sub)
    options, seen = [], set()
    for line in text.splitlines():
        if not line.strip().startswith("-"):
            continue
        o = _option_from_line(line)
        if not o or o["name"] in seen:
            continue
        seen.add(o["name"])
        seen.update(o["aliases"])
        options.append(o)
    positionals = []
    lines = text.splitlines()
    for k, line in enumerate(lines):
        m = USAGE.match(line)
        if not m:
            continue
        usage = m.group(1) or (lines[k + 1] if k + 1 < len(lines) else "")
        words = set(n["path"])
        for tok in re.findall(r"[\[<]?[A-Za-z][\w-]*[\]>]?(?:\.\.\.)?", usage)[1:]:
            bare = tok.strip("[]<>.")
            if not bare or bare in words or bare.upper() in GENERIC or tok.startswith("-"):
                continue
            if not (tok[0] in "[<" or bare.isupper()):
                continue
            if any(p["name"] == bare for p in positionals):
                continue
            kind = "file" if re.search(r"file|path|dir|input|output", bare, re.I) else "scalar"
            positionals.append({"name": bare, "required": tok[0] != "[", "repeated": tok.endswith("..."),
                                "kind": kind})
        break
    env = []
    for name in ENV_CLAP.findall(text):
        if name not in [e["name"] for e in env]:
            env.append({"name": name, "source": f"help/{n['slug']}"})
    for line in text.splitlines():
        if "env" in line.lower():
            for name in ENV_WORD.findall(line):
                if name not in [e["name"] for e in env]:
                    env.append({"name": name, "source": f"help/{n['slug']}"})
    return {"command": n["path"], "options": options, "positionals": positionals, "env_vars": env,
            "nonerror_exits": [], "uses_root_globals": False}


def load(sub):
    return read_json(sub_dir(sub, "interface.json"))


def load_root_globals(sub):
    """Root interface when this command's tests may carry root global options."""
    me = load(sub)
    if sub == "root" or not me or not me.get("uses_root_globals"):
        return None
    return load("root") or draft("root")


def errors(iface):
    errs = []
    if not isinstance(iface, dict):
        return ["interface.json must be an object"]
    for key, typ in (("command", list), ("options", list), ("positionals", list), ("env_vars", list),
                     ("nonerror_exits", list)):
        if not isinstance(iface.get(key), typ):
            errs.append(f"{key}: expected a {typ.__name__}")
    if errs:
        return errs
    spellings = {}
    for k, o in enumerate(iface["options"]):
        if not isinstance(o, dict) or not isinstance(o.get("name"), str) or not o["name"].startswith("-"):
            errs.append(f"options[{k}]: needs a 'name' starting with '-'")
            continue
        if o.get("takes_value", False) not in (False, "required", "optional"):
            errs.append(f"options[{k}] {o['name']}: takes_value must be false, \"required\" or \"optional\"")
        if not isinstance(o.get("context", False), bool):
            errs.append(f"options[{k}] {o['name']}: context must be true or false")
        if not isinstance(o.get("aliases", []), list):
            errs.append(f"options[{k}] {o['name']}: aliases must be a list")
            continue
        for s in [o["name"]] + list(o.get("aliases") or []):
            if s in spellings and spellings[s] != o["name"]:
                errs.append(f"spelling {s} belongs to both {spellings[s]} and {o['name']}")
            spellings[s] = o["name"]
    for k, p in enumerate(iface["positionals"]):
        if not isinstance(p, dict) or not p.get("name"):
            errs.append(f"positionals[{k}]: needs a name")
        elif p.get("kind", "scalar") not in ("scalar", "file", "text"):
            errs.append(f"positionals[{k}]: kind must be scalar, file or text")
    for k, e in enumerate(iface["env_vars"]):
        if not isinstance(e, dict) or not e.get("name"):
            errs.append(f"env_vars[{k}]: needs a name")
    if not isinstance(iface.get("interactive", False), bool):
        errs.append("interactive must be true or false")
    if not all(isinstance(x, int) and x != 0 for x in iface["nonerror_exits"]):
        errs.append("nonerror_exits: list of nonzero integers")
    return errs


def help_spellings_missing(sub, iface):
    """Option spellings appearing in the help text but absent from interface.json."""
    known = set()
    for o in iface.get("options", []):
        known.add(o["name"])
        known.update(o.get("aliases") or [])
    root = load_root_globals(sub)
    for o in (root or {}).get("options", []):
        known.add(o["name"])
        known.update(o.get("aliases") or [])
    missing = []
    for tok in SPELLING.findall(help_text(sub)):
        tok = tok.rstrip(".,:;)")
        base = tok.split("=", 1)[0]
        if base in ("-", "--") or base in known or base in missing:
            continue
        if re.match(r"^-\d", base):
            continue
        missing.append(base)
    return missing


def check(sub, quiet=False):
    iface = load(sub)
    if iface is None:
        return False, [f"missing {sub_dir(sub, 'interface.json')} (start from interface.draft.json)"], []
    errs = errors(iface)
    if not errs and iface["command"] != node(sub)["path"]:
        errs.append(f"command must be {node(sub)['path']}")
    warn = []
    if not errs:
        miss = help_spellings_missing(sub, iface)
        if miss:
            warn.append("in help but not in interface.json: " + ", ".join(miss))
    return not errs, errs, warn
