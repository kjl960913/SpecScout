#!/usr/bin/env python3
"""Drive the four steps of probe_spec_code6_mini with fresh mini-swe-agent agents.

    <mini venv>/bin/python drive4.py WORKSPACE --model openai/gpt-6-sol [--model-class litellm_response]
        [--docker-image programbench/<inst>:task_cleanroom_v6] [--parallel 5] [--from-step N]

WORKSPACE is a copy of the framework (pk, lib/, prompts/, templates/); state lives in WORKSPACE/work.
With --docker-image every ./pk command and every agent command runs inside one container started like
mini-swe-agent's ProgramBench runner (gold = /workspace/executable, docs = /workspace), with the
workspace mounted at /home/agent/pk4. Model calls stay on the host.

Step 1: ./pk discover, then one agent (inventory + docs index), check `./pk check 1`.
Step 2: per command node, ./pk iface draft, then an explorer agent (check `./pk check 2r SLUG`: it
        handed over with ./pk ready) followed by a review agent (check `./pk check 2 SLUG`: frozen by
        ./pk finish); nodes run in parallel (at most --parallel at a time). If the review agent fails
        twice the driver runs ./pk finish itself.
Step 3: per finished node, ./pk spec prepare, then one agent each, check `./pk check 3 SLUG`.
Step 4: one agent, check `./pk check 4`; in docker mode export and pack the submission.
An agent whose check fails is retried once with a fresh agent; a node that still fails is skipped
by the following steps.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
CFG = yaml.safe_load((HERE / "config4.yaml").read_text())
MOUNT = "/home/agent/pk4"
CLEAN_ENV_KEYS = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TZ")
BASE_ENV = {k: os.environ[k] for k in CLEAN_ENV_KEYS if k in os.environ}
BASE_ENV.setdefault("LANG", "C.UTF-8")
BASE_ENV["PYTHONUNBUFFERED"] = "1"
HELP_CAP = 12000


def official_environment():
    from minisweagent.config import builtin_config_dir
    return yaml.safe_load((builtin_config_dir / "benchmarks" / "programbench.yaml").read_text())["environment"]


class Container:
    """One long-lived container, started like the official DockerEnvironment, plus the workspace mount."""

    def __init__(self, ws, image, lifetime):
        self.ws, self.image, self.lifetime = ws, image, lifetime
        cfg = official_environment()
        self.docker, self.cwd = cfg.get("executable", "docker"), cfg["cwd"]
        self.run_args, self.env = list(cfg["run_args"]), dict(cfg.get("env") or {})
        self.name = "pk4-" + hashlib.sha1(ws.encode()).hexdigest()[:10]

    def running(self):
        r = subprocess.run([self.docker, "inspect", "-f", "{{.State.Running}}", self.name],
                           capture_output=True, text=True)
        return r.returncode == 0 and r.stdout.strip() == "true"

    def mounts_ws(self):
        """True when the running container's mount is this workspace directory itself (same inode): a
        workspace renamed away (.rN) and recreated at the same path keeps the same container name."""
        r = subprocess.run([self.docker, "exec", self.name, "stat", "-c", "%i", MOUNT],
                           capture_output=True, text=True)
        return r.returncode == 0 and r.stdout.strip() == str(os.stat(self.ws).st_ino)

    def ensure(self):
        if self.running() and self.mounts_ws():
            return
        subprocess.run([self.docker, "rm", "-f", self.name], capture_output=True)
        subprocess.run(["chmod", "-R", "a+rwX", self.ws], capture_output=True)
        subprocess.run([self.docker, "run", "-d", "--name", self.name, "-w", self.cwd, *self.run_args,
                        "-v", f"{self.ws}:{MOUNT}", self.image, "sleep", self.lifetime],
                       check=True, capture_output=True, text=True)
        self.exec('git config user.name "mini-swe-agent" && git config user.email "mini-swe-agent@proton.me"')

    def argv(self, command, cwd=None, env=None):
        cmd = [self.docker, "exec", "-w", cwd or self.cwd]
        for k, v in {**self.env, **(env or {})}.items():
            cmd += ["-e", f"{k}={v}"]
        return cmd + [self.name, "bash", "-lc", command]

    def exec(self, command, cwd=None, env=None, **kw):
        return subprocess.run(self.argv(command, cwd, env), text=True, capture_output=True,
                              encoding="utf-8", errors="replace", **kw)


def instance_id(image):
    m = re.match(r"(?:.*/)?(.+?)_1776_([^:]+)", image)
    return f"{m.group(1)}__{m.group(2)}" if m else re.sub(r"[/:]", "_", image)


class Driver:
    def __init__(self, a):
        self.a = a
        self.ws = str(Path(a.workspace).resolve())
        self.box = None
        if a.docker_image:
            BASE_ENV.clear()
            self.box = Container(self.ws, a.docker_image,
                                 a.container_lifetime or official_environment().get("container_timeout", "7h"))
        self.seen_ws = MOUNT if self.box else self.ws
        self.run_dir = Path(self.ws) / "mini_runs"
        self.run_dir.mkdir(exist_ok=True)
        self.lock = threading.Lock()
        self.journal = open(self.run_dir / "journal.jsonl", "a")
        prior = [json.loads(l).get("idx", 0) for l in open(self.run_dir / "journal.jsonl")
                 if '"agent_start"' in l]
        self.idx = max(prior or [0])
        self.failed = set()

    # -------------------------------------------------------------- plumbing
    def note(self, **kw):
        line = json.dumps({"t": dt.datetime.now().isoformat(timespec="seconds"), **kw}, ensure_ascii=False)
        with self.lock:
            self.journal.write(line + "\n")
            self.journal.flush()
            print(line, flush=True)

    def pk(self, args, timeout=3600):
        cmd = "./pk " + args
        if self.box:
            self.box.ensure()
            r = self.box.exec(cmd, cwd=MOUNT, timeout=timeout)
        else:
            r = subprocess.run(cmd, cwd=self.ws, shell=True, capture_output=True, text=True, env=BASE_ENV,
                               timeout=timeout, encoding="utf-8", errors="replace")
        return r.returncode, r.stdout + r.stderr

    def read(self, rel, default=""):
        p = Path(self.ws) / rel
        try:
            return p.read_text(errors="replace")
        except OSError:
            if self.box:
                r = self.box.exec(f"cat {shlex.quote(MOUNT + '/' + rel)}")
                if r.returncode == 0:
                    return r.stdout
            return default

    def read_json(self, rel, default=None):
        t = self.read(rel, "")
        try:
            return json.loads(t) if t else default
        except ValueError:
            return default

    def nodes(self):
        return (self.read_json("work/subcommands.json") or {}).get("nodes", [])

    def docs_for(self, slug):
        index = self.read_json("work/docs_index.json") or self.read_json("work/docs_index.draft.json") or {}
        rows = []
        for path, v in sorted(index.items()):
            cmds = v.get("commands") or []
            if not cmds or slug in cmds or slug is None:
                rows.append(f"- work/{path} ({v.get('size', '?')} bytes){': ' + v['summary'] if v.get('summary') else ''}")
        return "\n".join(rows) or "(none)"

    @staticmethod
    def cap(text):
        if len(text) > HELP_CAP:
            return text[:HELP_CAP] + f"\n... [{len(text) - HELP_CAP} more characters in the file]"
        return text

    def fill(self, name, values):
        text = (HERE.parent / "prompts" / f"{name}.md").read_text()
        missing = sorted(set(re.findall(r"\{\{(\w+)\}\}", text)) - set(values))
        if missing:
            raise SystemExit(f"prompt {name}: no value for placeholders {missing}")
        return re.sub(r"\{\{(\w+)\}\}", lambda m: str(values[m.group(1)]), text)

    # ---------------------------------------------------------------- agents
    def spawn(self, label, task, wall):
        from minisweagent.agents.default import DefaultAgent
        from minisweagent.environments.local import LocalEnvironment, _run
        from minisweagent.models import get_model

        with self.lock:
            self.idx += 1
            idx = self.idx
        scratch = self.run_dir / f"{idx:03d}.tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        scratch.chmod(0o777)
        seen_scratch = (MOUNT + str(scratch)[len(self.ws):]) if self.box else str(scratch)
        task = task.replace("@@SCRATCH@@", seen_scratch)
        (self.run_dir / f"{idx:03d}_{label}.task.md").write_text(task)
        box = self.box
        cmd_env = {"TMPDIR": seen_scratch} if box else {**BASE_ENV, "TMPDIR": seen_scratch}

        class Env(LocalEnvironment):
            def execute(self, action, cwd="", *, timeout=None):
                command = action.get("command", "")
                try:
                    if box:
                        result = subprocess.run(box.argv(command, cwd or self.config.cwd, cmd_env), text=True,
                                                encoding="utf-8", errors="replace", stdout=subprocess.PIPE,
                                                stderr=subprocess.STDOUT, timeout=timeout or self.config.timeout)
                    else:
                        result = _run(command, cwd or self.config.cwd, cmd_env, timeout or self.config.timeout)
                    output = {"output": result.stdout, "returncode": result.returncode, "exception_info": ""}
                except Exception as e:
                    raw = getattr(e, "output", None)
                    raw = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else (raw or "")
                    output = {"output": raw, "returncode": -1,
                              "exception_info": f"An error occurred while executing the command: {e}",
                              "extra": {"exception_type": type(e).__name__, "exception": str(e)}}
                self._check_finished(output)
                return output

        if self.a.fake_agent:
            # plumbing test: a scripted "agent" runs tests/fake/<step>.sh <slug> and finishes
            from minisweagent.models.test_models import make_output
            step, _, slug = label.replace("_retry", "").partition("_")
            cmd = f"cd {self.seen_ws} && bash {self.a.fake_agent}/{step}.sh {slug} 2>&1 | tail -40"
            model = get_model("deterministic", {"model_class": "deterministic", "outputs": [
                make_output("run script", [{"command": cmd}]),
                make_output("done", [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}])]})
        else:
            mcfg = dict(CFG["model"])
            if self.a.model_class:
                mcfg["model_class"] = self.a.model_class
            model = get_model(self.a.model, mcfg)
        env = Env(**dict(CFG["environment"], cwd=self.seen_ws))
        agent_cfg = dict(CFG["agent"], wall_time_limit_seconds=wall,
                         output_path=self.run_dir / f"{idx:03d}_{label}.traj.json")
        agent = DefaultAgent(model, env, **agent_cfg)
        self.note(event="agent_start", idx=idx, label=label)
        try:
            res = agent.run(task)
        except Exception as e:
            res = {"exit_status": type(e).__name__, "submission": str(e)[:500]}
        self.note(event="agent_done", idx=idx, label=label, exit_status=res.get("exit_status"),
                  cost=round(agent.cost, 4), calls=agent.n_calls)
        return res

    def with_retry(self, label, make_task, check, wall):
        """Run an agent; if its check fails, one fresh agent continues from the files on disk."""
        for attempt in (1, 2):
            note = "" if attempt == 1 else (
                "\nNOTE: a previous agent worked on this step and stopped before finishing. Its files and "
                "recorded tests are still in place; inspect them and continue from there.\n")
            self.spawn(label + ("" if attempt == 1 else "_retry"), make_task(note), wall)
            rc, out = check()
            self.note(event="check", label=label, attempt=attempt, ok=rc == 0, tail=out[-1500:])
            if rc == 0:
                return True
        return False

    # ----------------------------------------------------------------- steps
    def step1(self):
        rc, out = self.pk("discover /workspace/executable" if self.box else f"discover {self.a.gold}")
        self.note(event="discover", rc=rc, out=out[-3000:])
        if rc:
            raise SystemExit("discover failed")
        docs = self.read_json("work/docs_index.draft.json") or {}
        values = {"workspace": self.seen_ws, "scratch": "@@SCRATCH@@", "discover_summary": out.strip(),
                  "root_help": self.cap(self.read("work/help/root.txt")),
                  "docs_list": "\n".join(f"{p}  {v.get('size')}" for p, v in sorted(docs.items())) or "(none)"}
        ok = self.with_retry("step1", lambda note: self.fill("step1", dict(values, retry_note=note)),
                             lambda: self.pk("check 1"), CFG["wall_time"]["step1"])
        if not ok:
            raise SystemExit("step 1 failed twice")

    def _root_help_block(self, slug, needed):
        if slug == "root" or not needed:
            return ""
        return ("\nRoot command help (global options that may also appear in this command's invocations):\n\n```\n"
                + self.cap(self.read("work/help/root.txt")) + "\n```\n")

    def step2(self):
        nodes = self.nodes()
        for n in nodes:
            self.pk(f"iface draft {shlex.quote(n['slug'])}")
        root_draft = self.read_json("work/root/interface.draft.json") or {}
        root_globals = any(o["name"] not in ("--help", "--version") for o in root_draft.get("options", []))

        def one(n):
            slug, words = n["slug"], n["path"]
            values = {"workspace": self.seen_ws, "scratch": "@@SCRATCH@@", "sub": slug,
                      "sub_display": "executable" + "".join(" " + w for w in words),
                      "sub_words": " ".join(words) if words else "(none: this is the root command)",
                      "sub_words_example": "".join(w + " " for w in words),
                      "help_text": self.cap(self.read(f"work/{n['help_file']}")),
                      "root_help_block": self._root_help_block(slug, root_globals and len(nodes) > 1),
                      "docs_list": self.docs_for(slug)}
            q = shlex.quote(slug)
            if not self.pk(f"check 2r {q}")[0] == 0:
                ok = self.with_retry(f"step2_{slug}", lambda note: self.fill("step2", dict(values, retry_note=note)),
                                     lambda: self.pk(f"check 2r {q}"), CFG["wall_time"]["step2"])
                if not ok:
                    self.failed.add(slug)
                    return
            if self.pk(f"check 2 {q}")[0] == 0:
                return
            rvalues = {k: values[k] for k in ("workspace", "scratch", "sub", "sub_display", "help_text")}
            rvalues["docs_list"] = values["docs_list"].replace("\n", "; ")
            ok = self.with_retry(f"step2review_{slug}",
                                 lambda note: self.fill("step2_review", dict(rvalues, retry_note=note)),
                                 lambda: self.pk(f"check 2 {q}"), CFG["wall_time"]["step2_review"])
            if not ok:
                rc, out = self.pk(f"finish {q}")
                self.note(event="review_fallback_finish", slug=slug, rc=rc, tail=out[-1500:])
                if rc:
                    self.failed.add(slug)

        self._parallel(one, [n for n in nodes if n["slug"] not in self.failed])

    def step3(self):
        nodes = [n for n in self.nodes() if n["slug"] not in self.failed]

        def one(n):
            slug, words = n["slug"], n["path"]
            rc, out = self.pk(f"spec prepare {shlex.quote(slug)}")
            if rc:
                self.note(event="prepare_failed", slug=slug, out=out[-2000:])
                self.failed.add(slug)
                return
            iface = self.read_json(f"work/{slug}/interface.json") or {}
            units = self.read_json(f"work/{slug}/spec_input/units.json") or []
            values = {"workspace": self.seen_ws, "scratch": "@@SCRATCH@@", "sub": slug,
                      "sub_display": "executable" + "".join(" " + w for w in words),
                      "help_text": self.cap(self.read(f"work/{n['help_file']}")),
                      "root_help_block": self._root_help_block(slug, iface.get("uses_root_globals")),
                      "interface": json.dumps(iface, indent=1, ensure_ascii=False),
                      "findings": self.cap(self.read(f"work/{slug}/FINDINGS.md")),
                      "docs_list": self.docs_for(slug).replace("\n", "; "),
                      "units": "\n".join(f"{k}. `{u['unit']}`: read `{u['evidence']}`, write `{u['write']}`"
                                         for k, u in enumerate(units, 1))}
            ok = self.with_retry(f"step3_{slug}", lambda note: self.fill("step3", dict(values, retry_note=note)),
                                 lambda: self.pk(f"check 3 {shlex.quote(slug)}"), CFG["wall_time"]["step3"])
            if not ok:
                self.failed.add(slug)

        self._parallel(one, nodes)

    def step4(self):
        rows = []
        for n in self.nodes():
            disp = "executable" + "".join(" " + w for w in n["path"])
            if n["slug"] in self.failed:
                rows.append(f"- `{disp}`: no SPEC (an earlier step failed); use work/help/{n['slug']}.txt and the original")
            else:
                rows.append(f"- `{disp}`: work/{n['slug']}/SPEC.md")
        values = {"workspace": self.seen_ws, "scratch": "@@SCRATCH@@", "spec_list": "\n".join(rows)}

        def check():
            rc, out = self.pk("check 4")
            if rc:
                return rc, out
            rc2, out2 = self.pk("diff", timeout=7200)
            return rc2, out + out2

        if not self.with_retry("step4", lambda note: self.fill("step4", dict(values, retry_note=note)), check,
                               CFG["wall_time"]["step4"]):
            return
        # Repair rounds: while the self-test still fails and the last agent made progress, a fresh agent
        # continues on the remaining failures (at most --repair-rounds more agents).
        history = []
        for rnd in range(1, self.a.repair_rounds + 1):
            rc, out = self.pk("diff", timeout=7200)
            m = re.search(r"pass (\d+)/(\d+)", out)
            if rc or not m:
                self.note(event="repair_stop", round=rnd, reason="no successful build", tail=out[-1500:])
                return
            passed, total = int(m.group(1)), int(m.group(2))
            history.append(passed)
            progressed = len(history) == 1 or history[-1] > history[-2]
            self.note(event="repair_check", round=rnd, passed=passed, total=total, progressed=progressed)
            if passed == total or not progressed:
                return
            task = self.fill("step4_repair", dict(values, retry_note="", round=str(rnd),
                                                  max_round=str(self.a.repair_rounds), diff_summary=out.strip()))
            self.spawn(f"step4_repair{rnd}", task, CFG["wall_time"]["step4"])
            if self.pk("check 4")[0]:
                self.note(event="repair_stop", round=rnd, reason="check 4 failed after repair")
                return
        rc, out = self.pk("diff", timeout=7200)
        m = re.search(r"pass (\d+)/(\d+)", out)
        self.note(event="repair_done", passed=m and int(m.group(1)), total=m and int(m.group(2)))

    def _parallel(self, fn, items):
        with ThreadPoolExecutor(max_workers=max(1, self.a.parallel)) as pool:
            list(pool.map(fn, items))

    # ---------------------------------------------------------------- export
    def export(self):
        box = self.box
        r = box.exec("./pk export /workspace", cwd=MOUNT, timeout=600)
        if r.returncode:
            self.note(event="export_failed", tail=(r.stdout + r.stderr)[-2000:])
            return None
        box.exec("git add -A && git commit -qm 'probe_spec_code6_mini implementation'", timeout=600)
        dest = Path(self.ws) / "submission" / instance_id(box.image) / "submission.tar.gz"
        dest.parent.mkdir(parents=True, exist_ok=True)
        box.exec("tar -czf /tmp/_submission.tar.gz -C /workspace .", timeout=600)
        subprocess.run([box.docker, "cp", f"{box.name}:/tmp/_submission.tar.gz", str(dest)], check=True)
        check = box.exec("rm -rf /tmp/_verify && mkdir /tmp/_verify && cd /tmp/_verify && "
                         "tar -xzf /tmp/_submission.tar.gz && chmod +x ./compile.sh && ./compile.sh && "
                         "./executable --help </dev/null >/dev/null 2>&1; echo help_rc=$?", timeout=900)
        self.note(event="exported", submission=str(dest), compile_rc=check.returncode,
                  check=(check.stdout + check.stderr)[-500:])
        return dest

    def run(self):
        if self.box:
            self.box.ensure()
            self.note(event="container", name=self.box.name, image=self.box.image)
        state_path = self.run_dir / "driver_state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        self.failed = set(state.get("failed", []))
        steps = [(1, self.step1), (2, self.step2), (3, self.step3), (4, self.step4)]
        for k, fn in steps:
            if k < self.a.from_step or k > self.a.to_step:
                continue
            self.note(event="step_start", step=k)
            fn()
            state_path.write_text(json.dumps({"failed": sorted(self.failed), "done_step": k}))
            self.note(event="step_done", step=k, failed=sorted(self.failed))
        if self.box and self.a.to_step >= 4:
            self.export()
        self.note(event="done")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workspace")
    ap.add_argument("--model", default=os.environ.get("MINI_MODEL"))
    ap.add_argument("--model-class", default=None, help="e.g. litellm_response for gpt-6 models")
    ap.add_argument("--docker-image")
    ap.add_argument("--container-lifetime")
    ap.add_argument("--gold", help="local mode: path of the binary (docs are taken from its directory)")
    ap.add_argument("--parallel", type=int, default=5, help="agents running at the same time in steps 2 and 3")
    ap.add_argument("--repair-rounds", type=int, default=2,
                    help="extra step-4 agents while the self-test still fails and the previous agent made progress")
    ap.add_argument("--from-step", type=int, default=1)
    ap.add_argument("--to-step", type=int, default=4)
    ap.add_argument("--export-only", action="store_true")
    ap.add_argument("--fake-agent", metavar="DIR", help="plumbing test: scripted agents (DIR relative to the workspace)")
    a = ap.parse_args()
    if not a.model and not a.export_only and not a.fake_agent:
        sys.exit("set --model or MINI_MODEL")
    if not a.docker_image and not a.gold:
        sys.exit("give --docker-image (ProgramBench task image) or --gold (local binary)")
    d = Driver(a)
    if a.export_only:
        d.box.ensure()
        return 0 if d.export() else 1
    d.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
