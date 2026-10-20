"""Shared paths, JSON helpers, locking and slugs."""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile

FRAMEWORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def work_dir():
    return os.path.abspath(os.environ.get("PK_WORK") or os.path.join(os.getcwd(), "work"))


def wpath(*parts):
    return os.path.join(work_dir(), *parts)


def sha1(data):
    return hashlib.sha1(data).hexdigest()


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
        f.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def write_text(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def read_jsonl(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except FileNotFoundError:
        pass
    return rows


@contextlib.contextmanager
def locked(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def append_jsonl(path, row):
    with locked(path + ".lock"):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def slug(path):
    """Command path (list of words) -> directory name. [] -> 'root'."""
    if not path:
        return "root"
    return "~".join(_SAFE.sub(lambda m: "%%%02X" % ord(m.group(0)), p) for p in path)


def display(path):
    return "executable" + "".join(" " + p for p in path)


def nodes():
    """Command nodes from subcommands.json: [{path, slug, help_file, ...}]."""
    doc = read_json(wpath("subcommands.json"))
    if not doc:
        raise SystemExit("no work/subcommands.json -- run ./pk discover first")
    return doc["nodes"]


def node(sub):
    for n in nodes():
        if n["slug"] == sub:
            return n
    raise SystemExit(f"unknown command {sub!r}; known: " + ", ".join(n["slug"] for n in nodes()))


def sub_dir(sub, *parts):
    return wpath(sub, *parts)


def is_frozen(sub):
    return os.path.exists(sub_dir(sub, "clusters.json"))


def fail(msg, code=2):
    raise SystemExit(f"ERROR: {msg}" if code else msg)
