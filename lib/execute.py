"""Isolated execution of one test against a binary.

A test (the input half of a record) is:

    {"argv": [...],                       # arguments after the program name
     "files": {"name": "<sha1>" | "@dir" | "@noperm:<sha1>" | "@link:<target>"},
     "stdin": null | {"sha": "<sha1>"},   # bytes from the blob store
     "env": {"NAME": "value" | null},     # null unsets; @PROBE_DIR@/@PROBE_HOME@ expand
     "channel": "pipe" | "stdin" | "stdin_pipe" | "tty_out" | "tty" | "stdin_tty",
     "setup": [["argv", ...], ...]}       # earlier commands in the same directory

File names starting with "~/" are placed under the private HOME. Every execution
gets fresh <base>/w (cwd) and <base>/h (HOME); absolute paths of both are replaced
in the captured output by @PROBE_DIR@ / @PROBE_HOME@ so runs are comparable.
"""
import fcntl
import json
import os
import pty
import re
import resource
import select
import shutil
import signal
import stat as statmod
import struct
import subprocess
import sys
import tempfile
import termios
import time

from common import sha1, wpath

CHANNELS = {
    "pipe":       {"stdin": "devnull", "stdout": "pipe", "stderr": "pipe"},
    "stdin":      {"stdin": "file",    "stdout": "pipe", "stderr": "pipe"},
    "stdin_pipe": {"stdin": "pipe",    "stdout": "pipe", "stderr": "pipe"},
    "tty_out":    {"stdin": "devnull", "stdout": "tty",  "stderr": "tty"},
    "tty":        {"stdin": "tty",     "stdout": "tty",  "stderr": "tty"},
    "stdin_tty":  {"stdin": "pipe",    "stdout": "tty",  "stderr": "tty"},
}
WINSIZE = (24, 80)
TTY_TERM = "screen"
PIPE_TIMEOUT = 5.0
TTY_TIMEOUT = 2.0
FSIZE_MB = 8  # recorded output/file size cap
RLIMIT_FSIZE_MB = 1024  # kernel limit on files the program writes: launchers unpack embedded runtimes (ditaa: a
                        # 12.6 MB jar to /tmp) and failed with "write: Success" under an 8 MB RLIMIT_FSIZE
CPU_S = 10
PROBE_DIR = "@PROBE_DIR@"
PROBE_HOME = "@PROBE_HOME@"
ISOLATED_UNSET = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "OLDPWD")
ARGV0 = "executable"
NUMBER = re.compile(rb"\d+")


def mask_numbers(data):
    """Digit runs -> <N>: thread ids, PIDs, timestamps and the like."""
    return NUMBER.sub(b"<N>", data or b"")


# ------------------------------------------------------------------ blobs

class Blobs:
    def __init__(self, root=None):
        self.root = root or wpath("blobs")
        os.makedirs(self.root, exist_ok=True)

    def path(self, h):
        return os.path.join(self.root, h[:2], h)

    def put(self, data):
        h = sha1(data)
        p = self.path(h)
        if not os.path.exists(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".tmp-")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(tmp, 0o644)
            os.replace(tmp, p)
        return h

    def get(self, h):
        if h is None:
            return None
        try:
            with open(self.path(h), "rb") as f:
                return f.read()
        except OSError:
            return None


# ------------------------------------------------------------ filesystem

def _entry(full, blobs, cap):
    try:
        st = os.lstat(full)
    except OSError:
        return "gone"
    if statmod.S_ISLNK(st.st_mode):
        try:
            return "link:" + os.readlink(full)
        except OSError:
            return "link:?"
    if statmod.S_ISDIR(st.st_mode):
        return "dir"
    if not statmod.S_ISREG(st.st_mode):
        return "special"
    try:
        with open(full, "rb") as f:
            data = f.read(cap + 1)
    except OSError:
        return "unreadable"
    h = blobs.put(data[:cap])
    return "truncated:" + h if len(data) > cap else h


def snapshot(root, blobs, cap):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        rel = "" if rel == "." else rel
        for name in dirnames:
            out[os.path.join(rel, name)] = _entry(os.path.join(dirpath, name), blobs, cap)
        for name in filenames:
            out[os.path.join(rel, name)] = _entry(os.path.join(dirpath, name), blobs, cap)
    return out


def fsdiff(pre, post):
    created = {k: v for k, v in post.items() if k not in pre}
    modified = {k: v for k, v in post.items() if k in pre and pre[k] != v}
    deleted = sorted(k for k in pre if k not in post)
    return created, modified, deleted


def _inside(root, name):
    p = os.path.normpath(os.path.join(root, name))
    if not (p == root or p.startswith(root + os.sep)):
        raise ValueError(f"file name escapes the test directory: {name!r}")
    return p


def materialize(files, workdir, home, blobs):
    for name, ref in sorted((files or {}).items()):
        if name.startswith("~/"):
            p = _inside(home, name[2:])
        else:
            p = _inside(workdir, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if ref == "@dir":
            os.makedirs(p, exist_ok=True)
        elif ref.startswith("@link:"):
            if not os.path.lexists(p):
                os.symlink(ref[len("@link:"):], p)
        elif ref.startswith("@noperm:"):
            with open(p, "wb") as f:
                f.write(blobs.get(ref[len("@noperm:"):]) or b"")
            os.chmod(p, 0)
        else:
            data = blobs.get(ref)
            if data is None:
                raise ValueError(f"missing blob for file {name!r}: {ref}")
            with open(p, "wb") as f:
                f.write(data)


# ---------------------------------------------------------------- running

def _set_pty(fd, rows, cols):
    try:
        attrs = termios.tcgetattr(fd)
        attrs[1] &= ~termios.OPOST
        attrs[3] &= ~(termios.ECHO | termios.ECHONL)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except (termios.error, OSError):
        pass
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


def _preexec(fsize_mb, cpu_s, ctty):
    def f():
        cap = max(fsize_mb, RLIMIT_FSIZE_MB) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (cap, cap))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        if ctty:
            try:
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            except OSError:
                pass
    return f


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_process(binary, args, cwd, env, chan, stdin_data, timeout,
                fsize_mb=FSIZE_MB, cpu_s=CPU_S, winsize=WINSIZE, argv0=ARGV0):
    """One execution. Returns (rc, stdout, stderr, timed_out, capped)."""
    rows, cols = winsize
    cap = fsize_mb * 1024 * 1024
    child_fds, parent_fds = [], []
    readers, writer, ctty = {}, None, False
    inline = None
    mode = chan["stdin"]
    if mode == "tty":
        m, s = pty.openpty()
        _set_pty(s, rows, cols)
        cin, writer, ctty = s, m, True
        parent_fds.append(m)
        readers[m] = "stdout"
    elif mode == "pipe":
        r, w = os.pipe()
        cin, writer = r, w
        parent_fds.append(w)
    elif mode == "file":
        inline = tempfile.TemporaryFile()
        inline.write(stdin_data)
        inline.seek(0)
        cin = os.dup(inline.fileno())
    else:
        cin = os.open(os.devnull, os.O_RDONLY)
    child_fds.append(cin)
    std = {}
    for name in ("stdout", "stderr"):
        if chan[name] == "tty":
            m, s = pty.openpty()
            _set_pty(s, rows, cols)
            readers[m] = name
            parent_fds.append(m)
            child_fds.append(s)
            std[name] = s
        else:
            r, w = os.pipe()
            readers[r] = name
            parent_fds.append(r)
            child_fds.append(w)
            std[name] = w
    payload = stdin_data if mode in ("pipe", "tty") else b""
    if mode == "tty":
        payload += b"\x04\x04" if payload and not payload.endswith(b"\n") else b"\x04"

    timed_out, capped, rc, proc = False, False, None, None
    bufs = {"stdout": bytearray(), "stderr": bytearray()}
    try:
        try:
            proc = subprocess.Popen([argv0] + list(args), executable=binary, stdin=cin,
                                    stdout=std["stdout"], stderr=std["stderr"], cwd=cwd, env=env,
                                    close_fds=True, start_new_session=True,
                                    preexec_fn=_preexec(fsize_mb, cpu_s, ctty))
        except (OSError, ValueError) as e:
            return None, b"", f"<harness> {e}\n".encode(), False, False
        for fd in child_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        child_fds = []
        for fd in readers:
            os.set_blocking(fd, False)
        if writer is not None:
            os.set_blocking(writer, False)
        deadline = time.monotonic() + timeout
        wpos, done_writing, group_finished = 0, False, False
        while True:
            if not group_finished and proc.poll() is not None:
                _kill_group(proc)
                group_finished = True
                if writer is not None:
                    if writer not in readers:
                        os.close(writer)
                        parent_fds.remove(writer)
                    writer = None
                done_writing = True
                if not readers:
                    break
            left = deadline - time.monotonic()
            if left <= 0:
                timed_out = True
                _kill_group(proc)
                break
            wlist = [] if (writer is None or done_writing) else [writer]
            if not readers and not wlist:
                try:
                    proc.wait(timeout=left)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_group(proc)
                break
            try:
                rr, ww, _ = select.select(list(readers), wlist, [], min(0.1, max(left, 0.001)))
            except (OSError, ValueError):
                break
            for fd in ww:
                try:
                    wpos += os.write(fd, payload[wpos:wpos + 65536])
                except OSError:
                    wpos = len(payload)
                if wpos >= len(payload):
                    done_writing = True
                    if mode == "pipe":
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                        parent_fds = [x for x in parent_fds if x != fd]
                        writer = None
            for fd in rr:
                name = readers.get(fd)
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    readers.pop(fd, None)
                    if fd == writer:
                        writer = None
                        done_writing = True
                    parent_fds = [x for x in parent_fds if x != fd]
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                elif name:
                    room = cap + 1 - len(bufs[name])
                    if room > 0:
                        bufs[name] += chunk[:room]
                    if len(bufs[name]) > cap:
                        capped = True
                        _kill_group(proc)
            if capped:
                break
            if proc.poll() is not None and not readers:
                break
        if rc is None:
            try:
                rc = proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                rc = proc.wait()
                timed_out = True
    finally:
        if proc is not None:
            _kill_group(proc)
            proc.wait()
        if inline is not None:
            inline.close()
        for fd in child_fds + parent_fds:
            try:
                os.close(fd)
            except OSError:
                pass
    capped = capped or rc == -signal.SIGXFSZ
    if timed_out:
        rc = None
    return rc, bytes(bufs["stdout"])[:cap], bytes(bufs["stderr"])[:cap], timed_out, capped


def channel_name(test):
    if test.get("tui"):
        return "tui"
    ch = test.get("channel") or "pipe"
    if ch not in CHANNELS:
        raise ValueError(f"unknown channel {ch!r}; use one of {', '.join(CHANNELS)}")
    if ch == "pipe" and test.get("stdin"):
        ch = "stdin"
    return ch


def normalize(data, work, home):
    if not data:
        return data
    pairs = []
    for path, token in ((home, PROBE_HOME), (work, PROBE_DIR)):
        for v in {os.path.realpath(path), os.path.abspath(path)}:
            pairs.append((os.fsencode(v), token.encode()))
    for raw, token in sorted(pairs, key=lambda p: len(p[0]), reverse=True):
        data = data.replace(raw, token)
    return data


def build_env(test, work, home, chan):
    env = dict(os.environ)
    declared = test.get("env") or {}
    env["HOME"] = home
    for k in ISOLATED_UNSET:
        env.pop(k, None)
    for k, v in declared.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = str(v).replace(PROBE_HOME, home).replace(PROBE_DIR, work)
    if "tty" in chan.values() and "TERM" not in declared:
        env["TERM"] = TTY_TERM
    return env


def scratch_root():
    root = os.environ.get("PK_SCRATCH") or os.path.join(tempfile.gettempdir(), f"pk4-{os.getuid()}")
    os.makedirs(root, exist_ok=True)
    return root


TUI_LAUNCHER = r"""#!/bin/bash
cd "$PK_TUI_WORK" || exit 127
(exec -a executable "$PK_TUI_BIN" "$@")
echo $? > "$PK_TUI_EXIT"
exec sleep 100000
"""
TUI_CONF = "set -g default-terminal screen\n"


def run_tui(binary, args, work, env, tui):
    """Run inside a private tmux server: initial screen, then one screen capture after each key.

    tui = {"keys": [tmux key names, or "text:..." for literal text], "size": [cols, rows], "delay": s}.
    Returns (rc or None while still running, captures, b"", False, False, running)."""
    base = os.path.dirname(work)
    sock, exitf = os.path.join(base, "tmux.sock"), os.path.join(base, "exit")
    launcher, conf = os.path.join(base, "launch.sh"), os.path.join(base, "tmux.conf")
    with open(launcher, "w") as f:
        f.write(TUI_LAUNCHER)
    with open(conf, "w") as f:
        f.write(TUI_CONF)
    cols, rows = (tui.get("size") or [80, 24])[:2]
    delay = float(tui.get("delay") or 0.5)
    tenv = {k: v for k, v in env.items() if k not in ("TERM", "TMUX")}
    tenv.update(PK_TUI_WORK=work, PK_TUI_BIN=binary, PK_TUI_EXIT=exitf)
    tm = ["tmux", "-S", sock, "-f", conf]
    caps = []

    def capture():
        r = subprocess.run(tm + ["capture-pane", "-p", "-t", "t"], capture_output=True, env=tenv, timeout=10)
        return r.stdout

    try:
        subprocess.run(tm + ["new-session", "-d", "-x", str(cols), "-y", str(rows), "-s", "t",
                             "bash", launcher] + list(args), env=tenv, check=True, capture_output=True, timeout=10)
        time.sleep(delay)
        caps.append(capture())
        for key in tui.get("keys") or []:
            if key.startswith("text:"):
                cmd = tm + ["send-keys", "-t", "t", "-l", key[len("text:"):]]
            else:
                cmd = tm + ["send-keys", "-t", "t", key]
            subprocess.run(cmd, env=tenv, capture_output=True, timeout=10)
            time.sleep(delay)
            caps.append(capture())
    except (subprocess.SubprocessError, OSError) as e:
        caps.append(f"<harness> tmux failed: {e}\n".encode())
    finally:
        subprocess.run(tm + ["kill-server"], env=tenv, capture_output=True, timeout=10)
    rc, running = None, True
    try:
        with open(exitf) as f:
            rc, running = int(f.read().strip()), False
    except (OSError, ValueError):
        pass
    out = b"".join(f"--- screen {i} ---\n".encode() + c for i, c in enumerate(caps))
    return rc, out, b"", False, False, running


def run_once(binary, test, blobs, timeout=None):
    """Execute once. Returns the observation half of a record."""
    chname = channel_name(test)
    chan = CHANNELS["tty"] if chname == "tui" else CHANNELS[chname]
    if timeout is None:
        timeout = TTY_TIMEOUT if "tty" in chan.values() else PIPE_TIMEOUT
    cap = FSIZE_MB * 1024 * 1024
    base = tempfile.mkdtemp(prefix="t_", dir=scratch_root())
    work, home = os.path.join(base, "w"), os.path.join(base, "h")
    os.makedirs(work)
    os.makedirs(home)
    try:
        materialize(test.get("files"), work, home, blobs)
        env = build_env(test, work, home, chan)
        setup_exits = []
        for step in test.get("setup") or []:
            src, _, _, _, _ = run_process(binary, step, work, env, CHANNELS["pipe"], b"", timeout)
            setup_exits.append(src)
        stdin = test.get("stdin") or {}
        stdin_data = b""
        if stdin.get("sha"):
            stdin_data = blobs.get(stdin["sha"])
            if stdin_data is None:
                raise ValueError("missing stdin blob " + stdin["sha"])
        pre = snapshot(work, blobs, cap)
        home_pre = snapshot(home, blobs, cap)
        t0 = time.time()
        running = False
        if chname == "tui":
            rc, out, err, timed_out, capped, running = run_tui(binary, test["argv"], work, env, test["tui"])
        else:
            rc, out, err, timed_out, capped = run_process(binary, test["argv"], work, env, chan, stdin_data, timeout)
        wall = time.time() - t0
        out, err = normalize(out, work, home), normalize(err, work, home)
        created, modified, deleted = fsdiff(pre, snapshot(work, blobs, cap))
        outfiles = {n: h for n, h in sorted(created.items()) + sorted(modified.items())}
        outfiles.update({n: "<deleted>" for n in deleted})
        hc, hm, hd = fsdiff(home_pre, snapshot(home, blobs, cap))
        statefiles = {"~/" + n: h for n, h in sorted(hc.items()) + sorted(hm.items()) if h != "dir"}
        statefiles.update({"~/" + n: "<deleted>" for n in hd})
        capped = capped or any(str(h).startswith("truncated:") for h in outfiles.values())
        rec = {"channel": chname, "exit": rc,
               "stdout_sha": blobs.put(out), "stdout_len": len(out),
               "stderr_sha": blobs.put(err), "stderr_len": len(err),
               "stdout_msha": sha1(mask_numbers(out)), "stderr_msha": sha1(mask_numbers(err)),
               "outfiles": outfiles, "timeout": timed_out, "size_capped": bool(capped),
               "wall": round(wall, 3)}
        if chname == "tui":
            rec["tui_running"] = running
        if statefiles:
            rec["statefiles"] = statefiles
        if setup_exits:
            rec["setup_exits"] = setup_exits
        return rec
    finally:
        for dirpath, dirnames, filenames in os.walk(base):
            for n in dirnames + filenames:
                try:
                    os.chmod(os.path.join(dirpath, n), 0o700, follow_symlinks=False)
                except (OSError, NotImplementedError):
                    pass
        shutil.rmtree(base, ignore_errors=True)


def signature(rec, masked=None):
    """Observable identity. Records whose two runs differed only in numbers compare with digits masked."""
    if masked is None:
        masked = bool(rec.get("masked"))
    out = rec.get("stdout_msha", rec.get("stdout_sha")) if masked else rec.get("stdout_sha")
    err = rec.get("stderr_msha", rec.get("stderr_sha")) if masked else rec.get("stderr_sha")
    return (rec.get("exit"), out, err, json.dumps(rec.get("outfiles") or {}, sort_keys=True),
            bool(rec.get("timeout")), bool(rec.get("size_capped")))


def same_output(a, b):
    masked = bool(a.get("masked") or b.get("masked"))
    return signature(a, masked) == signature(b, masked)


def run_test(binary, test, blobs, twice=True):
    """Execute (twice when the first run completed) and flag nondeterminism."""
    first = run_once(binary, test, blobs)
    first["nondet"] = False
    if twice and not first["timeout"]:
        second = run_once(binary, test, blobs)
        if signature(first, False) != signature(second, False):
            first["repeat"] = {k: second[k] for k in ("exit", "stdout_sha", "stderr_sha", "outfiles")}
            if signature(first, True) == signature(second, True):
                # only numbers differ (thread id, PID, time): stable once digits are masked
                first["masked"] = True
            else:
                first["nondet"] = True
    return first


def stable(rec):
    finished = rec.get("exit") is not None or rec.get("tui_running")
    return (finished and not rec.get("timeout") and not rec.get("size_capped")
            and not rec.get("nondet"))
