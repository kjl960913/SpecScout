# probe_spec_code6_mini

Black-box reverse engineering of a CLI program in four agent steps. Given only a gold binary and its bundled
documentation (a ProgramBench task), a series of fresh [mini-swe-agent](https://mini-swe-agent.com) agents
explore the program's behaviour through a recording tool, turn the observations into one SPEC per
subcommand, and write a reimplementation that is differentially tested against the recorded gold behaviour.
The result is packed as a ProgramBench submission.

The full design (data formats, clustering rules, rationale for every gate) is in [DESIGN.md](DESIGN.md).
This README covers setup, running, monitoring, configuration and troubleshooting.

> Note: `launch4.sh` copies this whole directory, README.md and DESIGN.md included, into every run
> workspace, and the agents work in that directory. Never put credentials, hidden-test information or
> evaluation results into files of this framework.

---

## Contents

1. [Pipeline](#1-pipeline)
2. [Repository layout](#2-repository-layout)
3. [Requirements](#3-requirements)
4. [One-time setup](#4-one-time-setup)
5. [Running one instance](#5-running-one-instance)
6. [Driver options](#6-driver-options)
7. [Monitoring a run](#7-monitoring-a-run)
8. [Stopping, resuming and relaunching](#8-stopping-resuming-and-relaunching)
9. [Run outputs](#9-run-outputs)
10. [Evaluating a submission](#10-evaluating-a-submission)
11. [Running a batch of instances](#11-running-a-batch-of-instances)
12. [Configuration and tuning](#12-configuration-and-tuning)
13. [Execution environment and isolation](#13-execution-environment-and-isolation)
14. [`./pk` command reference](#14-pk-command-reference)
15. [Developing the framework](#15-developing-the-framework)
16. [Troubleshooting](#16-troubleshooting)

---

## 1. Pipeline

```
step 1  ./pk discover          -> agent: command inventory, docs index          -> ./pk check 1
step 2  per command: explore    -> agent: interface, tests, I/II/IV, checklist   -> ./pk ready SUB
                     review     -> agent: what the explorer missed               -> ./pk finish SUB
step 3  per command: SPEC       -> agent: unit JSON files, falsification         -> ./pk spec build SUB
step 4  implementation          -> agent: work/impl + ./pk diff loop             -> ./pk export /workspace
        repair rounds           -> agent: remaining diff failures (up to 2 more agents)
```

- **Step 1, discovery.** `./pk discover` walks the help tree, captures the raw help of every command node and
  copies the documentation. One agent checks the command inventory (`./pk node add/remove`) and writes a
  docs index.
- **Step 2, exploration.** For every command node (root included), in parallel: the script pre-fills
  `interface.draft.json` from help; an explorer agent fixes the interface, runs tests through `./pk t` /
  `./pk sweep` / `./pk batch`, records option relations (I dependent, II mutually exclusive, IV one dominates)
  and fills a feature checklist; `./pk coverage` adds rule tests and automatic controls and reports gaps;
  `./pk ready` enforces the coverage gate. A second, fresh review agent looks for what the explorer
  missed and freezes the node with `./pk finish`.
- **Step 3, SPEC.** For every frozen node, in parallel: `./pk spec prepare` writes one evidence file per
  unit (errors, exclusions, default, each option, combinations, env vars, features, overview); an agent
  writes the unit JSON files, tries to falsify the claims of option units with `./pk verify`, and renders
  `SPEC.md` with `./pk spec build`.
- **Step 4, implementation.** One agent reads all SPECs and docs, writes `work/impl/` with a `compile.sh`, and
  iterates on `./pk diff`, which compares the candidate byte for byte against the frozen gold observations.
  While failures remain and the previous round made progress, the driver starts up to `--repair-rounds`
  repair agents. Finally the driver exports and packs the submission.

Every gold call goes through `./pk t` (or `sweep` / `batch` / `verify`, which use the same machinery). The
test database is the single source of truth: it feeds the coverage report, the SPEC evidence and the
differential suite.

Number of agents = 1 (step 1) + 2 × nodes (explorer + reviewer) + nodes (step 3) + 1 (step 4)
+ up to 2 repair agents + at most one retry per failed agent.

---

## 2. Repository layout

```
pk                      single entry point (python3, standard library only); run it from the directory
lib/
  execute.py            run the gold: isolation, channels, pty, tmux (TUI), limits, blobs, normalisation
  testdb.py             tests.jsonl I/O, test ids, nondeterminism from the double run
  discover.py           step 1 help tree + docs copy + check
  iface.py              interface.json draft from help + validation
  argvparse.py          argv -> normalised options / env / positionals
  rules.py              automatic parser-level rule tests (2.6)
  cluster.py            clustering, controls, witnesses, filtering, freeze
  features.py           feature checklist, leak extraction, coverage gate
  sweep.py              ./pk sweep (word lists) and ./pk batch (candidate spellings)
  errtemplate.py        error templates (argv tokens become slots)
  specbuild.py          step 3 evidence files, unit validation, SPEC.md rendering, amend
  difftest.py           step 4 differential testing
  export.py             submission tree (impl/ + compile.sh wrapper)
  view.py               compact display of test records
  common.py             paths and JSON helpers
prompts/                task prompts, filled by the driver ({{placeholder}} syntax)
  step1.md  step2.md  step2_review.md  step3.md  step4.md  step4_repair.md
templates/
  examples/             one-shot examples of every step 3 unit JSON
  wordlists/            generic word lists for ./pk sweep (math functions, symbol names, unicode categories, ...)
driver/
  launch4.sh            start a docker-mode run for one ProgramBench instance
  drive4.py             the driver (mini-swe-agent agents, container, retries, repair rounds, export)
  config4.yaml          mini-swe-agent agent/model/environment config + per-step wall-clock limits
tests/
  test_pipeline.py      unit tests + end-to-end run of steps 1-4 on the mock CLI
  mock/mockq.py         small mock CLI (options, env vars, I/II/IV relations, subcommands)
  fake/step*.sh         scripted "agents" for checking the driver without model calls
DESIGN.md               design document
```

---

## 3. Requirements

**Host**

| Requirement | Notes |
|---|---|
| Linux, bash, `rsync`, `setsid`, `git` | used by `launch4.sh` |
| Docker | the user must be able to run `docker` without sudo; the driver starts one long-lived container per run |
| mini-swe-agent virtualenv | `launch4.sh` expects it at `../mini_driver/.venv` (next to this directory); tested with mini-swe-agent **2.4.6**, Python 3.13; it also provides `pyyaml` and `litellm` |
| Model API key | read by mini-swe-agent from `~/.config/mini-swe-agent/.env` (e.g. `OPENAI_API_KEY=...`) |
| ProgramBench task images | `programbench/<org>_1776_<name>.<commit>:task_cleanroom_v6`; pulled automatically by `launch4.sh` if missing |
| ProgramBench CLI (evaluation only) | an upstream checkout with `uv`, e.g. version 1.2.4; see [section 10](#10-evaluating-a-submission) |

**Inside the task image** (all ProgramBench task images satisfy this): `python3` (every `./pk` command runs
inside the container, so `pk` needs only the standard library of whatever Python the image ships), `bash`,
`git`, and `tmux` for interactive programs (`./pk t --tui`). The gold is `/workspace/executable` and the
bundled docs are under `/workspace`.

---

## 4. One-time setup

```sh
B=/data/kongjiaolong/ProgramBench_Guess_and_Check        # parent directory of this framework

# 1. mini-swe-agent venv at the path launch4.sh expects
uv venv $B/mini_driver/.venv --python 3.13
uv pip install --python $B/mini_driver/.venv/bin/python "mini-swe-agent==2.4.6"

# 2. API key (mini-swe-agent loads this file at import time)
mkdir -p ~/.config/mini-swe-agent
echo 'OPENAI_API_KEY=sk-...' >> ~/.config/mini-swe-agent/.env
chmod 600 ~/.config/mini-swe-agent/.env

# 3. check the framework itself (no docker, no model)
cd $B/probe_spec_code6_mini && python3 -m unittest -v tests/test_pipeline.py

# 4. optional: check driver + container + export on a real image with scripted agents (no model cost),
#    see section 15
```

The framework directory must be a clean git checkout: `launch4.sh` refuses to start when `git status`
shows uncommitted changes, so every run corresponds to a commit. Commit before launching.

---

## 5. Running one instance

```sh
driver/launch4.sh INSTANCE_ID MODEL [extra drive4.py arguments...]

driver/launch4.sh mgdm__htmlq.6e31bc8 openai/gpt-6-sol --model-class litellm_response
driver/launch4.sh sstadick__hck.b66c751 openai/gpt-5.5 --model-class litellm_response --parallel 3
```

- `INSTANCE_ID` is the ProgramBench instance id `<org>__<name>.<commit>`. The list of ids is the directory
  listing of `src/programbench/data/tasks/` in the ProgramBench checkout.
- `MODEL` is a litellm model name. The gpt-5.x / gpt-6 models need `--model-class litellm_response` (native
  Responses API). Without it the chat-completions bridge drops the tool calls and agents die with repeated
  format errors.

What `launch4.sh` does:

1. Derives the image `programbench/<org>_1776_<name>.<commit>:task_cleanroom_v6` and the workspace
   `../<framework dir>_<name>_<model tag>_docker` (e.g. `../probe_spec_code6_mini_htmlq_sol_docker`;
   the framework dir is the name of this directory). The model tag is the model name without the
   provider prefix and without a leading `gpt-6-`, so `openai/gpt-6-sol` becomes `sol` and
   `openai/gpt-5.5` stays `gpt-5.5`.
2. Refuses to start if the framework has uncommitted changes or the workspace already exists.
3. Pulls the image if it is not present locally.
4. Copies the framework into the workspace (`rsync`, without `.git`, `__pycache__`, `work`, `tests`).
5. Starts `driver/drive4.py` detached (`setsid nohup`) under a clean environment
   (`env -i` with only `PATH HOME USER LOGNAME SHELL LANG`), writing `driver.out` and `driver.pid` in the
   workspace. It prints the workspace, the image and the pid, then returns immediately.

A typical small instance (one command node) takes 1-3 hours. Instances with many subcommands take longer;
steps 2 and 3 run up to `--parallel` agents at once.

**Local mode** (no docker, gold run directly on the host): call the driver yourself with `--gold`:

```sh
rsync -a --exclude .git --exclude __pycache__ --exclude work ./ ../pk6_local_ws/
cd ../pk6_local_ws && ../mini_driver/.venv/bin/python driver/drive4.py . --model openai/gpt-6-sol \
    --model-class litellm_response --gold /path/to/binary
```

Documentation is taken from the binary's directory. In local mode the gold and all agent commands run
directly on the host (no network or filesystem isolation, host Python and tools) and no submission is
exported, so docker mode is the normal way to run.

---

## 6. Driver options

`drive4.py WORKSPACE [options]` (`launch4.sh` passes extra arguments through):

| Option | Default | Meaning |
|---|---|---|
| `--model NAME` | `$MINI_MODEL` | litellm model name (required unless `--fake-agent` / `--export-only`) |
| `--model-class CLASS` | mini default | e.g. `litellm_response` for gpt-5.x / gpt-6 models |
| `--docker-image IMAGE` | — | docker mode: ProgramBench task image (launch4.sh always sets it) |
| `--gold PATH` | — | local mode: the binary to explore (one of `--docker-image` / `--gold` is required) |
| `--parallel N` | 5 | agents running at the same time in steps 2 and 3 |
| `--repair-rounds N` | 2 | extra step 4 agents while the self-test still fails and the previous round made progress |
| `--from-step N` / `--to-step N` | 1 / 4 | run only steps N..M (see [section 8](#8-stopping-resuming-and-relaunching)) |
| `--export-only` | off | docker mode: only export + pack `work/impl` again |
| `--container-lifetime T` | `7h` (official config) | `sleep` duration of the container (it is started with `--rm`) |
| `--fake-agent DIR` | — | scripted agents from `DIR/<step>.sh`, relative to the workspace (plumbing test) |

---

## 7. Monitoring a run

```sh
W=../probe_spec_code6_mini_htmlq_sol_docker
tail -f $W/driver.out                     # the journal, one JSON line per event
(cd $W && python3 pk status)              # per node: phase, number of tests, coverage gaps
ls $W/mini_runs/                          # NNN_<label>.task.md (prompt sent) and .traj.json (trajectory)
```

`./pk status` reads only `work/`, so it can be run on the host while the run is going.

Journal events (`mini_runs/journal.jsonl`, mirrored in `driver.out`):

| Event | Meaning |
|---|---|
| `container` | container name and image |
| `discover` | output of `./pk discover` |
| `step_start` / `step_done` | step boundaries; `step_done` lists the failed nodes |
| `agent_start` / `agent_done` | one agent: index, label (`step2_root`, `step2review_root`, `step3_root`, `step4`, `step4_repair1`, `..._retry`), exit status, cost, number of model calls |
| `check` | completion check after an agent (`ok`, last output lines) |
| `review_fallback_finish` | the review agent failed twice and the driver ran `./pk finish` itself |
| `prepare_failed` | `./pk spec prepare` failed for a node |
| `repair_check` / `repair_stop` / `repair_done` | step 4 repair rounds: pass counts and why they stopped |
| `exported` / `export_failed` | submission path, compile result of the packed tarball |
| `done` | the driver finished |

Cost and calls of a run so far:

```sh
python3 - $W/mini_runs/journal.jsonl <<'EOF'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1])]
done = [r for r in rows if r["event"] == "agent_done"]
print(f"agents {len(done)}  calls {sum(r.get('calls') or 0 for r in done)}  "
      f"cost ${sum(r.get('cost') or 0 for r in done):.2f}  finished {any(r['event'] == 'done' for r in rows)}")
EOF
```

Other useful views inside a workspace (all run from the workspace directory):
`./pk tests SUB` (one line per recorded test), `./pk features SUB [--open]` (checklist),
`./pk coverage SUB` (gap report; safe to rerun before freeze), `./pk diff` (after step 4 has written
`work/impl`). In docker mode run them inside the container for exactly the agents' view:
`docker exec -w /home/agent/pk4 <container> ./pk status`.

---

## 8. Stopping, resuming and relaunching

**Stop a run.** The driver is a session leader (started with `setsid`), so kill its process group, then
remove the container:

```sh
kill -- -$(cat $W/driver.pid)
C=pk4-$(python3 -c 'import hashlib,os,sys;print(hashlib.sha1(os.path.realpath(sys.argv[1]).encode()).hexdigest()[:10])' $W)
docker rm -f $C                     # container name = pk4- + sha1(absolute workspace path)[:10]
```

**Resume in the same workspace.** Everything lives on disk (`work/`, `mini_runs/driver_state.json`), so the
driver can be restarted with `--from-step`, using the same clean environment as `launch4.sh`:

```sh
cd $W && setsid nohup env -i PATH="$PATH" HOME="$HOME" USER="$USER" LOGNAME="$USER" SHELL=/bin/sh LANG=C.UTF-8 \
    ../mini_driver/.venv/bin/python driver/drive4.py . --model openai/gpt-6-sol --model-class litellm_response \
    --docker-image programbench/<org>_1776_<name>.<commit>:task_cleanroom_v6 --from-step 3 \
    >> driver.out 2>&1 < /dev/null & echo $! > driver.pid
```

What each step does when it is re-entered:

| Step | On re-entry |
|---|---|
| 1 | reruns `./pk discover` and the inventory agent |
| 2 | skips nodes that are already frozen; a node that is `ready` but not frozen gets only the review agent |
| 3 | reruns `spec prepare` and a new SPEC agent for **every** node not marked failed |
| 4 | starts a new implementation agent (it sees the existing `work/impl`), then repair rounds and export |

Nodes recorded as failed in `driver_state.json` stay skipped. Agent indices continue from the journal.
`--to-step 2` stops after exploration (useful for studying exploration alone); `--export-only` repacks
the current `work/impl`.

**Relaunch from scratch.** `launch4.sh` refuses to overwrite a workspace. Keep the old one by renaming it
(convention: `.r1`, `.r2`, ..., or `.invalid` for broken runs), then launch again:

```sh
mv $W $W.r1 && driver/launch4.sh <INSTANCE_ID> <MODEL> --model-class litellm_response
```

The new run gets a new container even though the path (and so the container name) is the same: the driver
checks that the container mounts the same directory inode and otherwise recreates it.

---

## 9. Run outputs

```
<workspace>/
  driver.out  driver.pid           driver log (= journal) and pid
  mini_runs/
    journal.jsonl                  event log
    driver_state.json              {"failed": [...], "done_step": N}
    NNN_<label>.task.md            the prompt actually sent to agent NNN
    NNN_<label>.traj.json          its full trajectory (messages, tool calls, outputs, cost)
    NNN.tmp/                       its scratch directory (TMPDIR)
  submission/<instance_id>/submission.tar.gz      the packed submission (docker mode)
  work/
    bin/executable                 staged gold
    subcommands.json  help/  docs/  docs_index.json
    fixtures/<sub>/                input files created by agents
    blobs/                         content-addressed stdout/stderr/files of every test
    <sub>/                         per node: interface.json, tests.jsonl, relations.json, features.json,
                                   FINDINGS.md, coverage.json, clusters.json, suite.json, spec_input/,
                                   sections/, SPEC.md, entry_index.json, spec_changes.jsonl
    impl/                          the reimplementation (source + compile.sh + executable)
    diff/<run>/                    results of each ./pk diff
```

`SPEC.md` of each node is the human-readable output of steps 2-3. `work/impl/NOTES.md` (when present) is
the step 4 agent's account of what still fails and why.

The submission tarball holds `/workspace` after export: the original docs, `impl/` (a copy of
`work/impl` without build products) and a generated `compile.sh` that builds `impl/` and installs
`./executable` as a launcher. During export the driver unpacks it in a temporary directory, runs
`compile.sh` and `./executable --help`, and logs the result in the `exported` event.

---

## 10. Evaluating a submission

Evaluation uses the ProgramBench CLI (upstream checkout, e.g. 1.2.4 at `/tmp/pb_src_v124`). Its run
directory must contain `<instance_id>/submission.tar.gz`, which is exactly the workspace's `submission/`:

```sh
cd /tmp/pb_src_v124
PROGRAMBENCH_DOCKER_RUN_TIMEOUT=1800 uv run programbench eval $W/submission -b 2
uv run programbench info $W/submission            # score per instance
```

- The directory name must be the exact instance id, or the harness silently skips it
  ("No instances to evaluate").
- `PROGRAMBENCH_DOCKER_RUN_TIMEOUT` (default 300 s) is too short on a loaded host or for slow test
  branches; 1800 is a safe value. Keep `-b` (branch workers) low (2-3) when the host is busy.
- The result is written to `<instance_id>/<instance_id>.eval.json`. The score `programbench info` prints is
  a pass rate over the active, non-ignored tests.
- Use ProgramBench 1.2.x. Versions before the pytest-rerunfailures pin (e.g. 1.0.2) count rerun attempts
  of failing tests as passes and inflate scores.

The step 4 self-test (`./pk diff`) only covers the behaviour the framework observed; hidden tests are
usually lower. Compare the two to see whether a gap comes from exploration or from implementation.

---

## 11. Running a batch of instances

`launch4.sh` returns immediately, so a batch is just a loop plus a waiter per instance. The pattern used
so far: launch a group, wait for each driver to log `done` (or die), evaluate, append one result line.

```sh
#!/bin/bash
B=/data/kongjiaolong/ProgramBench_Guess_and_Check; FW=probe_spec_code6_mini
MODEL=openai/gpt-6-sol; TAG=sol; OUT=$B/results.txt
name_of() { local n=${1#*__}; echo ${n%.*}; }
wait_eval() {
  local iid=$1; local W=$B/${FW}_$(name_of $iid)_${TAG}_docker
  while ! grep -q '"event": "done"' $W/mini_runs/journal.jsonl 2>/dev/null && kill -0 $(cat $W/driver.pid) 2>/dev/null; do
    sleep 60; done
  (cd /tmp/pb_src_v124 && PROGRAMBENCH_DOCKER_RUN_TIMEOUT=1800 uv run programbench eval $W/submission -b 2 > $W/submission/eval.log 2>&1)
  echo "$(name_of $iid) | $(cd /tmp/pb_src_v124 && uv run programbench info $W/submission 2>&1 | grep __ | head -1)" >> $OUT
}
echo "=== $(date '+%F %T') $FW @ $(git -C $B/$FW rev-parse --short HEAD) $MODEL" >> $OUT
for iid in mgdm__htmlq.6e31bc8 rs__jplot.2a54bcc; do
  $B/$FW/driver/launch4.sh $iid $MODEL --model-class litellm_response && wait_eval $iid &
done
wait
```

Tips:

- Record the framework commit with every result (as above); the workspace itself does not record it.
- Each run uses `--parallel` agents in steps 2/3 plus `docker exec` processes, and each container may use up
  to 20 CPUs and 60 GB of memory. Launch 4-5 instances at a time on a shared host.
- In zsh, write `programbench/${iid}:task_cleanroom_v6`, not `$iid:task...`, or `:t` is taken as a
  history modifier.

---

## 12. Configuration and tuning

**`driver/config4.yaml`**

| Key | Value | Meaning |
|---|---|---|
| `agent.step_limit`, `agent.cost_limit` | 0, 0 | no limits: agents decide how much to explore |
| `agent.max_consecutive_format_errors` | 5 | an agent stops after this many responses without a valid tool call |
| `agent.system_template` | — | rules shared by all agents (no decompiling, no internet, no env dumps, finish command) |
| `environment.timeout` | 1800 | seconds per bash command of an agent |
| `model.model_kwargs` | `drop_params`, `timeout: 1200` | passed to litellm |
| `model.cost_tracking` | `ignore_errors` | models missing from litellm's price table are allowed (cost then shows 0) |
| `model.observation_template` | — | outputs over 30k characters are cut to head + tail |
| `wall_time.*` | step1 1 h, step2 4 h, step2_review 2 h, step3 4 h, step4 6 h | per-agent wall-clock safety limit |

**Prompts** (`prompts/*.md`) use `{{placeholder}}` variables filled by `drive4.py`. A placeholder without a
value aborts the driver. `@@SCRATCH@@` is replaced with the agent's scratch directory.

**Constants in `lib/`** (change them, then rerun the unit tests and commit):

| Constant | Value | Effect |
|---|---|---|
| `execute.PIPE_TIMEOUT` / `TTY_TIMEOUT` | 5 s / 2 s | per gold run, by channel |
| `execute.CPU_S` | 10 | RLIMIT_CPU of a gold run |
| `execute.FSIZE_MB` | 8 | recorded output / file size cap (`size_capped`) |
| `execute.RLIMIT_FSIZE_MB` | 1024 | kernel limit on files the program writes |
| `execute.WINSIZE`, `TTY_TERM` | 24×80, `screen` | pty size and TERM on tty channels |
| `difftest.CAND_TIMEOUT` | 10 s | per candidate run in `./pk diff` |
| `testdb.default_jobs()` | min(8, CPUs) | parallel test runs in `./pk diff` (`--jobs` overrides) |
| `features.MIN_FAILED` | 8 | distinct failed inputs before a checklist item may be marked unreachable |
| `sweep.MAX_WORDS` / `MAX_CANDIDATES` | 600 / 200 | per `./pk sweep` / `./pk batch` |
| `sweep.MAX_BATCH_REJECTED` | 6 | rejected batch candidates kept in the SPEC and suite |
| `discover.DOC_MAX` | 256 KB | larger files are not copied to `work/docs/` |
| `specbuild.MAX_DEFAULT`, `MAX_ERR_MEMBERS`, ... | 20, 30, ... | how much evidence goes into each step 3 evidence file |

---

## 13. Execution environment and isolation

**Container.** One container per run, started like mini-swe-agent's official ProgramBench environment
(`benchmarks/programbench.yaml` from the installed mini-swe-agent): `--rm --network none --cpus 20
--memory 60g --memory-swap 60g --user agent --cap-drop SYS_PTRACE`, cwd `/workspace`, plus the workspace
mounted at `/home/agent/pk4`. The agents' bash commands and all `./pk` calls run inside it via
`docker exec`; only model calls happen on the host. Consequences:

- No network inside: implementations can only use what the image already has (its Python version and
  standard library, compilers, ...). Check with
  `docker run --rm --entrypoint sh <image> -c 'python3 --version; which gcc cargo go'`.
- The container user (uid 1000) owns files the agents create. See [section 16](#16-troubleshooting) for
  cleanup.

**Environment.** The driver runs under `env -i`. In docker mode, agent commands get only the official
environment variables (`PAGER`, `LESS`, ...) and `TMPDIR`; the API key stays in the driver process.

**Each gold run** (`./pk t` and friends) gets a fresh working directory and HOME, XDG variables unset,
resource limits, process-group cleanup, a pty for tty channels and a private tmux server for `--tui`.
It runs **twice**: different results mark it `nondet`; differences only in digits are accepted as
`masked`. Probe directories in outputs are normalised to `@PROBE_DIR@` / `@PROBE_HOME@`.

**What agents see.** The prompt for their step (`mini_runs/NNN_*.task.md`), the workspace (framework files,
including this README and DESIGN.md, plus `work/`), `/workspace` with the gold and docs, and the outputs of
their own commands. The system template forbids decompiling or tracing the gold, searching for its source,
editing the framework, and printing environment variables.

---

## 14. `./pk` command reference

Run from the workspace directory (`./pk --help` lists everything). Program arguments of `t`, `sweep`,
`batch` and `verify` must come after a lone `--`.

| Step | Command | Purpose |
|---|---|---|
| 1 | `discover [BINARY] [--docs DIR] [--maxdepth N]` | help tree, raw help bytes, docs copy (driver) |
| 1 | `node add WORD...` / `node remove SLUG` | fix the command inventory (agent) |
| 1 | `check 1` | validate step 1 artefacts |
| 2 | `iface draft SUB` / `iface check SUB` | pre-fill (driver) / validate interface.json, warn about help options missing from it |
| 2 | `t SUB [conditions] [--tag T] [--covers ID] [--note ...] -- ARGS` | run the gold twice and record one test |
| 2 | `sweep SUB --list NAME \| --list-file F --covers vocab:X [conditions] -- ARGS-with-{W}` | one test per word of a word list |
| 2 | `batch SUB --cands F \| --cand TEXT --covers ID [--split] [conditions] -- ARGS-with-{C}` | try many candidate spellings at once (≤200) |
| 2 | `features SUB [--open]` | checklist status |
| 2 | `tests SUB [--source PREFIX] [--sweep RUN] [--all]` | one line per recorded test |
| 2 | `rules SUB [--force]` | rule tests (run automatically by the first `coverage`) |
| 2 | `coverage SUB` | controls, witnesses, gaps, checklist gaps |
| 2 | `ready SUB` / `finish SUB` | explorer hand-over (coverage gate) / reviewer freeze |
| 2 | `check 2r SUB` / `check 2 SUB` | ready / frozen (driver checks) |
| 3 | `spec prepare SUB` | write evidence files (driver) |
| 3 | `spec check SUB [--unit U]` / `spec build SUB` | validate units / render SPEC.md + entry_index.json |
| 3 | `verify SUB --option NAME --assert ID --why TEXT [conditions] -- ARGS` | falsification run of one Assert |
| 3 | `check 3 SUB` | SPEC built and valid |
| 4 | `t SUB --tag impl ... -- ARGS` | run the gold without adding to the suite |
| 4 | `diff [--entry E \| --test ID] [--limit N] [--jobs N]` | build `work/impl` and compare with the frozen suite |
| 4 | `spec amend SUB UNIT --cite TEST --reason TEXT` | correct a SPEC unit, citing a gold test |
| 4 | `check 4` | `work/impl/compile.sh` exists |
| 4 | `export DEST` | write the submission tree (driver) |
| any | `status` | progress per node |

Test conditions shared by `t`, `sweep`, `batch` and `verify`:

| Condition | Meaning |
|---|---|
| `--file NAME[=SRC]` | put `work/fixtures/<sub>/SRC` into the test directory as NAME (repeatable) |
| `--stdin FILE` / `--stdin-text TEXT` | stdin from a fixture / given text (`\n` escapes) |
| `--env K=V` | extra environment variable (repeatable; `@PROBE_DIR@` / `@PROBE_HOME@` allowed) |
| `--channel C` | `pipe` (default, stdin=/dev/null), `stdin`, `stdin_pipe`, `tty_out`, `tty`, `stdin_tty` |
| `--setup 'ARGV'` | run a gold command first in the same directory (stateful programs; repeatable) |
| `--tui 'KEYS'` `[--tui-size 80x24] [--tui-delay 0.5]` | interactive mode in tmux: one screen capture after each key |

---

## 15. Developing the framework

- **Unit + end-to-end tests** (no docker, no model, about 15 s):
  `python3 -m unittest -v tests/test_pipeline.py`. They run steps 1-4 on `tests/mock/mockq.py`.
- **Driver plumbing on a real image** (no model cost). `launch4.sh` excludes `tests/`, so make a
  workspace that includes them:

  ```sh
  rsync -a --exclude .git --exclude __pycache__ --exclude work ./ ../pk6_fake_ws/
  cd ../pk6_fake_ws && ../mini_driver/.venv/bin/python driver/drive4.py . --fake-agent tests/fake \
      --docker-image programbench/mgdm_1776_htmlq.6e31bc8:task_cleanroom_v6
  ```

  The scripted agents in `tests/fake/` run a few `./pk` commands per step and finish, which exercises the
  container, checks, retries, repair logic and export.
- **Changing behaviour**: prompts, rules and constants are all in this directory. Update DESIGN.md when a
  mechanism changes, run the tests, commit, then launch (launch4.sh requires a clean tree).
- **Inspecting an agent**: `mini_runs/NNN_<label>.task.md` is the exact prompt; the `.traj.json` has every
  message and command output.

---

## 16. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `framework has uncommitted changes` | commit (or stash) changes in this directory before `launch4.sh` |
| `<workspace> already exists` | rename the old workspace (`.r1`, ...) or delete it |
| Agents stop at once with repeated format errors (gpt-5.x / gpt-6) | add `--model-class litellm_response` |
| `set --model or MINI_MODEL` | give `--model` |
| Image reported missing in zsh scripts | write `${iid}:task_cleanroom_v6`; zsh treats `$iid:t` as a modifier |
| Permission denied when deleting a workspace | files were created by the container user (uid 1000). While the container runs: `docker exec <container> chmod -R a+rwX /home/agent/pk4`; afterwards: `docker run --rm --user 0 -v $W:/w --entrypoint chmod <image> -R a+rwX /w` |
| A node is listed under `failed` | its step 2 or step 3 agent failed twice. Step 2 failure skips the node in step 3; step 3 failure leaves it without SPEC in step 4 (the implementer gets only its help). Check the `check` events and the trajectory |
| `review_fallback_finish` event | the review agent failed twice; the driver froze the node itself (the gate still applies) |
| Step 4 ended with self-test failures | expected when repair rounds stop making progress; see `work/impl/NOTES.md` and `./pk diff` |
| `export_failed` or `compile_rc` ≠ 0 | `work/impl/compile.sh` does not build in a clean copy; fix it and run `drive4.py ... --export-only` |
| Evaluation reports `No instances to evaluate` | the directory under `submission/` must be the exact instance id |
| Evaluation times out / `results_read_failed` | raise `PROGRAMBENCH_DOCKER_RUN_TIMEOUT`, lower `-b`, pre-pull the evaluation image |
| Implementation passes `./pk diff` locally but fails in evaluation | `./pk diff` runs inside the same task image, so check for behaviour outside the suite (hidden tests are broader) rather than environment differences |
| The driver died (no `done` event, pid gone) | read the end of `driver.out`, then resume with `--from-step` ([section 8](#8-stopping-resuming-and-relaunching)) |
