# probe_spec_code6_mini design document

> Status: code4 implemented (2026-10-03); code5 adds a coverage gate to step 2 on top of it (2026-10-04, see section 12); code6 adds batch probing with `pk batch` (2026-10-04, see section 13). For usage see README.md.

---

## 0. Goals and principles

### 0.1 Goal

Given a CLI program that comes only with a gold binary and its accompanying documentation, use mini-swe-agent to drive a number of agents that:
systematically explore its behaviour, organise the observations into one SPEC per subcommand, and finally write a reimplementation with identical behaviour,
which is differentially tested against the recorded gold observations.

### 0.2 Design principles

- **Four major steps, a new agent for each step**; steps 2 and 3 use one agent per subcommand and can run in parallel.
- **Agents explore and judge; scripts record, match, group and validate**.
- **Every gold call goes through the single test-recording tool `./pk t`**; the test database is the only source of truth for the whole pipeline.
- **No hand-offs**; every agent's input is trimmed to its subcommand, evidence is deduplicated by output signature, and outputs are truncated.
- **No hidden-option scan**, **no full set of pairs** (both would pollute the mini-swe-agent context).
- Keep the protocol light: read files, write files, run one validation command at the end; no claim/submit state machine and no environment fingerprint.

---

## 1. Overall flow

```
Step 1  Discovery          script discover → [agent ×1] check subcommands, build doc index
            │  subcommands.json, help/, docs/, docs_index.json
            ▼
Step 2  Explore + cluster  for each subcommand (in parallel):
            │   script pre-fills interface draft → [agent ×1] review interface → free exploration 2.1–2.5, 2.L (incl. I/II/IV)
            │   → ./pk coverage (on the first call the script picks a base automatically and runs the 2.6 rule tests)
            │   → 1 round of follow-up tests → ./pk finish (final clustering + filtering, frozen)
            │  interface.json, tests.jsonl, relations.json, FINDINGS.md, clusters.json
            ▼
Step 3  Write SPEC         for each subcommand (in parallel):
            │   ./pk spec prepare (generates the evidence file of each unit) → [agent ×1] writes JSON unit by unit,
            │   picks Asserts of option units itself for falsification → ./pk spec build
            │  sections/*.json, SPEC.<sub>.md, verify tests, entry_index.json
            ▼
Step 4  Implement + diff   [agent ×1] reads all SPECs + docs, may run the gold,
                           writes code → ./pk diff (differential run on the frozen suite) → fix code or ./pk spec amend
                           → export submission
```

Number of agents = 1 + 2 × (number of subcommand nodes, including root) + 1 (plus failure retries, see section 8).

---

## 2. Directory layout

### 2.1 Framework code (`probe_spec_code4_mini/`)

```
pk                      single entry point (python3, standard library)
DESIGN.md               this document
prompts/
  step1.md  step2.md  step3.md  step4.md     task prompts of the four agent kinds (templates, variables filled by the driver)
lib/
  execute.py            execution layer (isolation, channels, pty, blobs, output normalisation, double run)
  testdb.py             test database I/O, ID allocation, nondet verdict from the double run
  iface.py              interface.json validation + draft pre-filled from help
  argvparse.py          argv → normalised option set / env set / positionals
  rules.py              2.6 rule-test generation
  cluster.py            2.7 clustering, witness verdicts, automatic controls, filtering
  errtemplate.py        error templates (argv tokens turned into slots)
  specbuild.py          step 3 evidence files, unit JSON validation, SPEC.md rendering
  verify.py             falsification (run + record; the judgement is the agent's)
  difftest.py           step 4 differential testing
  export.py             export of the ProgramBench submission
  view.py               compact display of test records (pk t output, evidence files)
templates/
  examples/             one-shot examples of each unit JSON (the SPEC.md render format lives in lib/specbuild.py)
driver/
  drive4.py             mini-swe-agent driver (docker mode)
  config4.yaml
  launch4.sh
tests/
  test_pipeline.py      unit tests + end-to-end test running steps 1–4 on a mock CLI
  mock/mockq.py         small CLI for tests (options, env, I/II/IV, subcommands)
  fake/step{1..4}.sh    scripted "fake agents" to check the driver without calling a model (drive4.py --fake-agent tests/fake)
```

### 2.2 Workspace (one per instance, `work/`)

```
work/
  bin/executable              staged gold (produced by discover)
  subcommands.json            step 1
  help/<sub>.txt              raw help bytes of each node
  docs/                       docs copied from /workspace (text <256KB, except LICENSE/COPYING)
  docs_index.json             written by the step 1 agent
  fixtures/<sub>/             input files created by the step 2 agent
  <sub>/                      one directory per subcommand (root is called root)
    interface.draft.json      pre-filled by script
    interface.json            after review by the step 2 agent
    tests.jsonl               all tests of this subcommand (agent/rule/control/verify)
    blobs/ -> ../blobs        global content-addressed store (shared)
    relations.json            I/II/IV (written by the step 2 agent)
    FINDINGS.md               written by the step 2 agent
    coverage.json             latest coverage result
    clusters.json             final clustering (frozen at finish)
    suite.json                test ids kept after filtering (= part of the differential suite)
    spec_input/NN-<unit>.md   step 3 evidence files (generated by script)
    sections/<unit>.json      written by the step 3 agent
    SPEC.md                   rendered result
    entry_index.json          test_id → SPEC entry
    spec_changes.jsonl        record of spec amendments in step 4
  blobs/                      global blob store
  impl/                       step 4 implementation (src, compile.sh)
  diff/<run>/                 result of each differential run
  submission/                 exported artefacts
```

---

## 3. Shared infrastructure

### 3.1 Test-recording tool `./pk t`

Every gold call must go through it (mandatory in steps 2 and 3; recommended in step 4).

```
./pk t <sub> [options] -- ARGV...

  ARGV                 all arguments after the program name (including the subcommand tokens)
  --file NAME[=SRC]    put fixtures/<sub>/SRC (default: same name) into the test working directory as NAME; repeatable
  --stdin FILE         stdin comes from a file in fixtures
  --stdin-text STR     stdin is the given text (\n escapes supported)
  --env K=V            extra environment variable; repeatable. V may use @PROBE_DIR@ / @PROBE_HOME@
  --channel C          pipe (default) | stdin | stdin_pipe | tty_out | tty | stdin_tty
  --setup "ARGV"       first run one gold command in the same directory (for stateful programs); repeatable
  --tui "KEYS"         interactive mode (channel=tui): runs in a private tmux, captures the screen once at start and
                       once after each key (tmux key names, text:abc for literal text); stdout is recorded as the
                       screens joined by `--- screen N ---`; still running after the last key records
                       `tui_running: true` (counted as success, stable)
  --tui-size 80x24 / --tui-delay 0.5
  --note "..."         what this test is meant to check (one sentence, read later when writing the SPEC)
  --tag 2.1|2.2|...    exploration subtask label (written into source)
```

**Execution**: each test has its own workdir and HOME (w/h/s layout), a timeout
(pipe 5s, tty 1s), RLIMIT_FSIZE, process-group cleanup, a pty for tty (TERM=screen), probe directories in the output normalised to
`@PROBE_DIR@`/`@PROBE_HOME@`, and an fsdiff of written files. **Every test runs twice automatically**; if the signatures differ it gets `nondet: true`.

**Record** (appended to `work/<sub>/tests.jsonl`):

```json
{
  "id": "root-t0123",
  "sub": "root",
  "argv": ["-t", "p"],
  "files": {"page.html": "<sha1>"},
  "stdin": {"kind": "file", "name": "page.html", "sha": "<sha1>"},
  "env": {"HTMLQ_X": "1"},
  "channel": "stdin",
  "setup": [],
  "exit": 0,
  "stdout_sha": "...", "stdout_len": 120,
  "stderr_sha": "...", "stderr_len": 0,
  "outfiles": {},
  "nondet": false, "timeout": false, "size_capped": false,
  "source": "agent:2.1",
  "note": "verify -t prints text content"
}
```

Values of `source`: `agent:2.1`…`agent:2.5`, `agent:2.3` (I/II/IV construction), `rule:<phase>`, `rule:control`,
`verify:<option>:<assert_id>`, `impl` (step 4, not added to the suite).

**Output returned to the agent** (compact):

```
root-t0123  exit=0  channel=stdin
stdout (120B):
<first 2000 characters>
stderr (0B)
files written: (none)
[nondet]  ← only shown when unstable
full: work/blobs/<sha>
```

### 3.2 `interface.json`

The script pre-fills a draft from help and the step 2 agent reviews and completes it. It is the common basis for argv parsing, the coverage denominator and rule-test generation.

```json
{
  "command": ["query"],
  "options": [
    {"name": "--list", "aliases": ["-l"], "takes_value": false,
     "source": "help", "note": ""},
    {"name": "--base", "aliases": ["-b"], "takes_value": "required",
     "value_forms": ["separate", "equals"], "repeatable": false, "source": "help"}
  ],
  "positionals": [
    {"name": "keywords", "required": false, "repeated": true,
     "kind": "scalar"}
  ],
  "env_vars": [
    {"name": "_ZO_DATA_DIR", "source": "docs/README.md:L120"}
  ],
  "nonerror_exits": [],
  "uses_root_globals": false
}
```

- `takes_value`: `false` | `"required"` | `"optional"`; `value_forms`: `separate` / `equals` / `attached`.
- `positionals[].kind`: `scalar` | `file` | `text` (decides the set of boundary values for 2.value).
- The base invocation of the rule tests and the representative values of options are not filled in by the agent; the script picks them from existing tests by rule (see 5.4).
- `nonerror_exits`: "non-error" exit codes of grep/diff-like programs (e.g. `[1]`).
- `uses_root_globals`: whether root global options appear in this subcommand's tests (decides whether step 3 gets the root help).
- `options[].context`: **context option** marker, decided by the step 2 agent. Only set it when all three hold: (a) its only effect is to say where input is read from or
  output is written to (`-f/--file/--input FILE`, `-o/--output FILE`, `-C/--directory DIR`); (b) removing it and providing the
  same input another way (stdin, a positional) or taking the same output from stdout gives the same result; (c) the value is a location and the file content is only data. Options whose file content changes the processing (`--config`,
  `--rules`, `--dictionary` FILE) and any option that changes format/filtering/sorting/encoding are not marked; when unsure, do not mark.

**Validation** (`./pk iface check <sub>`): fields valid; aliases do not conflict;
**the raw help of this subcommand is scanned with a regex, and every `-x`/`--xxx` that appears but is not in interface.json raises a warning**.

### 3.3 argv parser (`lib/argvparse.py`)

Input: one test + interface.json (the sub's; overlaid with root's if `uses_root_globals`). Output:

```json
{"options": {"--text": 1}, "values": {"--base": ["x"]},
 "positionals": ["p"], "unknown": [], "env": ["HTMLQ_X"], "parse_ok": true}
```

Rules: aliases are normalised to `name`; `--opt=val`, `-ovalue` and combined short options `-abc` are recognised; an option with `takes_value` consumes the next
token; everything after `--` is positional; unrecognised `-x` goes into `unknown`; tests whose argv does not start with the sub path (e.g. 1.nosub)
get `parse_ok: false`. Option values do not take part in grouping and are only attributes within a group. Repeated occurrences are counted.

### 3.4 Documentation

- The step 1 script copies text files under `/workspace` smaller than 256KB (except LICENSE/COPYING and the gold itself) to `work/docs/`.
- The step 1 agent writes `docs_index.json`:

```json
{"docs/README.md": {"size": 18234, "subcommands": ["root", "query"],
                    "summary": "usage, env vars _ZO_*, examples"}}
```

- The step 2/3 prompts only give the **paths** of the docs relevant to the subcommand; the agent reads the relevant passages with grep/sed.

---

## 4. Step 1: discovery

**Script**: `./pk discover /workspace/executable`
- Stages it as `work/bin/executable` (argv0 = executable), walks the help tree, runs each candidate
  subcommand to confirm it, and captures the raw help bytes into `work/help/`.
- Copies docs to `work/docs/`.
- Does not extract each subcommand's option details (left to step 2).

**Agent (×1) task**:
1. Check `subcommands.json`: fill gaps against help and docs (subcommands mentioned in docs but not listed in the help tree must be run to confirm) and remove false positives.
2. Write `docs_index.json`.

Every node (including root and intermediate nodes) gets its own agent in both step 2 and step 3; pure dispatch nodes are not treated differently.

**Artefacts**: `subcommands.json` (`path` and `help_file` of each node), `help/`, `docs/`, `docs_index.json`.

**Completion check** `./pk check 1`: valid JSON; every node has a help file; docs_index covers all files in docs/.

---

## 5. Step 2: per-subcommand exploration and clustering

### 5.1 Agent context

| Content | How |
|---|---|
| Task prompt (rules, 2.1–2.5 requirements, I/II/IV definitions, channel table, corner-case requirements) | prompt |
| Raw help of this subcommand | prompt |
| `interface.draft.json` | prompt |
| Paths of relevant docs (from docs_index) | prompt (paths only) |
| Root help (only when this sub is a subcommand and root has global options) | prompt |
| Tool command reference (`./pk t / iface check / coverage / finish`) | prompt |

### 5.2 Order of work

1. **Review interface**: correct the draft against help and docs; add options mentioned in docs; write `interface.json`; `./pk iface check`.
2. **Free exploration (2.1–2.5, plus 2.L)**: explore item by item as the prompt requires; example commands in the docs are turned into tests first. 2.L (added 2026-10-03): when an argument is itself a
   small language (selector, expression, regex, format string, query language, field list), test its syntax constructs one by one (those named in help/docs + the standard
   constructs of that language), each with at least one successful test, and record the error messages of malformed forms. Reason: the test databases of both htmlq runs had 0 pseudo-class tests; the
   pseudo-class code of the second implementation crashed and the self-test diff could not catch it (−34 on hidden tests). Extended on 2026-10-04 to the input data formats the program parses (json-tui's hidden tests compare
   nlohmann's parse error text one by one, and exploration had only tried 3 malformed inputs). 2.T interactive mode: the agent decides whether it is needed and uses `--tui` if so.
3. **I/II/IV**: the agent combines options freely, decides which pairs may be dependent, mutually exclusive or overriding, builds the tests itself and writes `relations.json`.
4. **coverage**: when exploration is done run `./pk coverage <sub>`. **On the first call** the script first picks a base from existing tests and runs the 2.6
   rule tests (see 5.4), then clusters and decides witnesses; the report gives both the rule-test summary and the gaps.
5. **1 round of follow-up tests**: test the gaps (and unexpected behaviour exposed by the rule tests) → write `FINDINGS.md` → `./pk finish <sub>`.

### 5.3 Prompt outline (draft, English)

```text
You explore ONE command of a black-box CLI: `{sub_display}`. Every call of the gold binary MUST go through
`./pk t {sub} ... -- ARGV`; never run work/bin/executable directly (unrecorded calls are lost).
Never disassemble/strace the binary. Read docs only with grep/sed/head on the listed paths.

Goals, in this order:
1. Fix interface.json (start from interface.draft.json): every option in help AND docs, aliases,
   whether it takes a value, positionals, env vars named in help/docs, nonerror_exits.
   Run `./pk iface check {sub}`.
2. Explore (parser-level edge cases -- arity, empty arguments, option spellings, missing option values,
   misspelled command names -- are generated automatically later; do not repeat them by hand):
   2.1 Make every option's function visibly happen at least once: a successful run whose output differs
       from the same run without that option. Choose inputs that can reveal the option's effect.
   2.2 For every environment variable named in help/docs, make its effect visible the same way
       (set vs unset, everything else identical).
   2.3 Option pairs (2-way only). Definitions, always within one identical context:
       - I (A depends on B): A alone shows no effect, B alone has an effect, and A+B succeeds with output
         different from B alone.
       - II (mutually exclusive): A alone succeeds, B alone succeeds, but A+B and B+A both fail.
       - IV (one dominates): A+B succeeds and its output equals the output of A alone (or of B alone).
       Decide yourself which pairs could plausibly be II or IV (help wording such as "overrides",
       "conflicts with", "ignored when", last-wins value options, --help/--version with anything).
       For each candidate run base, A, B, A+B, B+A and record the verdict in relations.json.
       Do NOT enumerate all pairs.
   2.4 Channels. The same invocation may behave differently when stdin carries data or when
       stdout/stderr is a terminal:
         pipe (stdin=/dev/null) | stdin (file) | stdin_pipe | tty_out (stdout+stderr tty) |
         tty (all tty) | stdin_tty (stdin pipe, output tty)
       Check a plain working invocation and the options likely to depend on it (color, paging, REPL, progress)
       on the other channels; keep exploring on channels that change something.
   2.5 Corner cases:
       • Boundary probing: testing inputs at the limits of accepted ranges (e.g., empty input,
         maximum-length strings, special characters) to determine exact boundary semantics that
         documentation leaves implicit.
       • Error-path elicitation: systematically triggering error conditions (malformed input, missing
         required arguments, conflicting flags) to record the precise stderr message and exit code
         each condition produces.
       • Output-format refinement: presenting adjacent inputs and comparing their outputs to resolve
         ambiguities in field ordering, delimiter choice, and whitespace handling that prose
         descriptions inevitably leave underspecified.
   2.L Languages and input formats: (a) an argument/option value that is a small language (selector,
       expression, regex, format string, query/filter language, field list); (b) a structured input format
       the program parses (JSON, YAML, CSV, XML/HTML, config files, source code, binary formats such as
       ELF/gzip). Test construct by construct -- named in help/docs plus the standard constructs -- each
       with a successful test, plus one malformed input per kind of error (exact messages, positions).
   2.T Interactive mode: the agent decides whether the command is interactive (full-screen UI, waits for
       keys); if so it sets interface.json "interactive": true and drives it with ./pk t --tui.
3. When exploration is done run `./pk coverage {sub}` (the first call also runs the automatic parser-level
   tests and summarises them). Fix the reported gaps once, write FINDINGS.md, then `./pk finish {sub}`.
Give every test a --note saying what it checks.
```

### 5.4 Rule tests (2.6)

Triggered automatically by the first `./pk coverage` (can also be run on its own with `./pk rules <sub>`; runs only once).

#### 5.4.1 Automatic choice of base

Rule tests need a working base invocation. The agent has already run many tests in 2.1–2.5, and the script picks one of them by rule:

**Candidates**: agent tests of this sub (`source` is `agent:*`) that are stable (not nondet/timeout/size_capped),
exit 0, `parse_ok` with no unknown token, channel pipe / stdin / stdin_pipe (not tty), and no extra env.

**Ranking** (compared in order):
1. fewest options (zero options preferred) — the variants add options on top of it;
2. has actual output (non-empty stdout or files written);
3. most often used as a witness control (the more witness pairs share its positionals / input files / stdin, the more
   likely options take effect on this input);
4. has input (input file or stdin) preferred;
5. number of positionals closest to the number declared in interface (so skip / empty have something to vary);
6. earliest id.

**Representative option values** (used by 3.variant / 3.novalue): the value used most often in that option's witness tests; without a witness, the value from any
successful test where it appears; failing both, the placeholder `x`.

**No candidate found**: only the phases that do not depend on a base are run (1.global / 1.sub / 1.nosub), and the report says why.
The chosen base and representative values are written to `work/<sub>/rules_base.json`.

#### 5.4.2 Phases

Generated and run relative to the base:

| Phase | Content |
|---|---|
| `1.global` (root only) | bare, `--help`/`-h`, `--version`/`-V`, unknown subcommand, unknown subcommand + help, `--`, unknown flag |
| `1.sub` (subcommands only) | case changes, capitalised, truncated, extra trailing character, repeated, `--`, `--help`/`-h`, garbage token |
| `1.nosub` (subcommands only) | each option without the subcommand |
| `2.arity` `2.skip` `2.empty` | 0..n+1 positionals, skip the i-th, the i-th as an empty string |
| `2.value` | fixed boundary values of positionals (`value_policy.fixed_values`; for file kinds `-`, missing, directory, no permission, dangling link) |
| `2.name` | same content, different file name (for extension-sensitive programs) |
| `2.bytes` | byte boundaries of input file/stdin (empty, no trailing newline, CRLF, non-UTF-8) |
| `3.variant` | for each option: aliases, `=` form, attached, single dash, upper case, repeated, truncated, extra trailing character, flag given a value |
| `3.novalue` | three shapes of a value-taking option missing its value: swallow / trailing / next_opt |


Returns a summary (individual tests are not echoed):

```
rules: 86 tests (1.sub 10, 2.arity 4, 2.empty 2, 2.value 9, 3.variant 47, 3.novalue 14)
  success 31, error 55, nondet 0
distinct error templates (8):
  exit 2  "error: Found argument '{argv}' which wasn't expected..."   ×21
  exit 2  "error: The argument '{argv}' requires a value..."           ×14
  ...
```

### 5.5 Coverage report (generated by script)

`./pk coverage <sub>`: provisional clustering → automatic controls → witness verdicts → (first call: pick base, run rule tests, re-cluster) → print the rule-test summary and the gaps.

```
interface check: ok
⚠ in help but not in interface.json: --color, -Z
options triggered 9/12, env 1/2
gaps:
  --base         4 tests, all identical to their controls (inert)
  --detect-base  appears only in failing tests
  -w             no test
  env HTMLQ_X    no test
relations.json: 2 entries (II 1, IV 1); referenced tests all present
```

Denominator = number of options in interface.json (an option and its aliases count as one) + number of env_vars.

### 5.6 `relations.json` (written by the agent)

```json
[
  {"a": "--sep", "b": "--number", "class": "I",
   "tests": {"base": "root-t0010", "a": "root-t0015", "b": "root-t0016", "ab": "root-t0017"},
   "reason": "separator only matters when lines are numbered"},
  {"a": "--output", "b": "--filename", "class": "II",
   "tests": {"base": "root-t0010", "a": "root-t0011", "b": "root-t0012",
             "ab": "root-t0013", "ba": "root-t0014"},
   "reason": "both orders exit 1 with 'cannot be used with'"},
  {"a": "--text", "b": "--attribute", "class": "IV", "dominant": "--attribute",
   "tests": {"base": "...", "a": "...", "b": "...", "ab": "...", "ba": "..."},
   "reason": "A+B output equals --attribute alone"}
]
```

Script validation: test ids exist, class ∈ {I, II, IV}, IV has `dominant`; I is checked mechanically against the definition above (tests contain at least a, b, ab).
For II/IV the judgement itself is not re-checked.

### 5.7 `FINDINGS.md` (written by the agent, used by step 3)

```markdown
## Behaviour summary          (3–10 key points)
## Not triggered               (each option/env not triggered + reason)
## Uncertain                   (behaviour observed but whose rule is unclear)
## Channels                    (which channels changed the behaviour)
```

### 5.8 Clustering (2.7, shared by `./pk finish` and `./pk coverage`)

**Step 0 unstable**: `nondet` / `timeout` / `size_capped` → `unstable`. When the two runs differ **only in numbers** (thread ids, PIDs, timestamps, etc.)
the test is not nondet: it is recorded as `masked: true`, treated as "stable after masking digits", and grouped as usual; comparisons (witness/control, error templates, diff) replace
runs of digits in stdout/stderr with `<N>` before comparing, while exit codes and written files are still compared strictly. Every record stores `stdout_msha` / `stderr_msha`
(hashes after masking). Reason: Rust panic messages carry thread ids, so all of htmlq's invalid selectors (exit 101) were judged unstable and filtered out, and the implementer never saw the evidence.

**Step 1 success vs error**: `exit == 0` or `exit ∈ nonerror_exits` → success; otherwise → error.

**Step 2 parsing**: argvparse gives (option set, env set).

**Step 3a grouping of successful tests** (key = exact set; values do not take part):

| Condition | Class |
|---|---|
| env set non-empty | `env:X` (`env:X+Y` with several env vars) |
| option set (after removing context options) empty | `default` |
| {A} | `option:A` |
| ≥2 options | `combo:A+B[+...]` |
| `parse_ok: false` or contains `unknown` | `misc` |

**Step 3b error tests**: all go into one `errors` class; **semantic grouping is left to the step 3 agent**. The script only pre-groups as an aid:
after replacing argv tokens appearing in stderr (and stdout when needed) with slots and normalising probe directories, templates
`T01`, `T02`… are formed by `(exit, template)`; each test records its template and its own option set. Templates are only for display and evidence compression, not the final classification.

**context option**: removed from the option set before grouping; when matching controls it and its value must stay exactly the same (treated as a background condition like input files and stdin),
so `-f x -t div` belongs to `option:--text` and its control is `-f x div`. A context option does not need a witness: appearing in
a successful test counts as triggered (coverage shows `triggered (context option ...)`); its own `option:A` class keeps at most 10 successful tests that use it
with mutually different outputs, as evidence for the SPEC unit. The file-kind variants of the rule tests (missing, directory, no permission, dangling link, renamed, byte boundaries)
also apply to the file value of context options.

**Step 4 automatic controls**: for each test in `option:A` without a control, the script removes A's tokens (including its value) from argv,
keeps everything else unchanged and runs it once, `source = rule:control`. The same is done for `env:X` by removing that variable.

**Control matching conditions**: same sub, same channel, same files (name→sha), same stdin, same setup, same remaining env, same positionals,
option set exactly S−{A} with the remaining option values identical.

**Relation I (dependency)**: some options only have an effect when another option is present (e.g. a separator option only matters when a numbering option
is on), so single-option tests are always inert. Like II/IV, this relation is built and judged by the agent combining pairs itself, and written to relations.json
(`class: "I"`, `a` is the dependent, `b` the host). The script checks it against the definition: **A has no witness of its own, B does; tests.ab is tests.b plus A,
with everything else identical, succeeds (no error), and its output differs from B alone**. When it passes, A counts as `triggered only with B`
(shown in the coverage report), and the ab/b pair is kept in the `option:A` class as A's dependent witness (`dependent` field);
I entries that do not satisfy the definition are listed with their problems in the relations line of the coverage report.

**Step 5 witness verdict (script only)**: test on the success side + has a control + neither is nondet + signatures
(exit, stdout, stderr, outfiles) differ → witness. Otherwise inert (identical signature) or no_control.

**Step 6 filtering (applies to both the SPEC evidence and the differential suite)**:

| Class | Kept |
|---|---|
| `option:A` | witness tests + their controls |
| `env:X` | witness tests + their controls |
| `default` | all |
| `combo:*` | only those referenced by relations.json |
| `errors` | all |
| any test referenced by relations.json | always kept (even if its class would filter it) |
| `unstable` / `misc` | all dropped |

**Step 7 dedup (display only, does not affect the suite)**: tests with identical signatures within a class form one output signature;
the step 3 evidence files show only 1–2 representatives per signature.

**Artefacts**:

`clusters.json`

```json
{"sub": "root", "frozen_at": "...",
 "clusters": [
   {"id": "option:--text", "kind": "option", "options": ["--text"],
    "kept": ["root-t0123", "root-t0099"], "dropped": ["root-t0200"],
    "witness": [{"test": "root-t0123", "control": "root-t0099"}],
    "inert": ["root-t0200"], "signatures": 3,
    "error_templates_involving": ["T03"]},
   {"id": "errors", "kind": "error", "kept": ["..."],
    "templates": [
      {"id": "T03", "exit": 1,
       "template": [{"literal": "error: The argument '"}, {"argv_index": 0}, {"literal": "' requires..."}],
       "members": ["root-t0050", "..."], "options_seen": [["--base"], ["--output"]]}
    ]}
 ],
 "coverage": {"options": "9/12", "env": "1/2", "gaps": ["--base", "-w", "HTMLQ_X"]}}
```

`suite.json`: all test ids kept after filtering.

**`./pk finish <sub>` checks**: interface valid; coverage run at least once; relations valid; FINDINGS.md exists;
then final clustering and filtering, writes clusters.json / suite.json and freezes (afterwards only verify tests may be appended to this sub's tests.jsonl).

---

## 6. Step 3: per-subcommand SPEC writing

### 6.1 Agent context (only this subcommand)

| Content | How |
|---|---|
| Task prompt, SPEC unit formats, one-shot of each unit | prompt |
| Raw help, interface.json, FINDINGS.md of this subcommand | prompt |
| Unit list (`spec_input/` file list + test count/signature count of each file) | prompt |
| Root help (only when `uses_root_globals: true`) | prompt |
| Paths of relevant docs | prompt (paths only) |
| Evidence of each unit | agent reads `spec_input/NN-<unit>.md` in order |
| gold | only via `./pk verify` / `./pk t` |

### 6.2 Evidence files (generated by `./pk spec prepare <sub>`)

One markdown file per unit, numbered in writing order. Example `04-option--text.md`:

```markdown
# option --text (-t)   witness 3, signatures 2
help: "-t, --text   Output only the contents of text nodes inside selected elements"

## witness root-t0123   [agent:2.1] note: verify -t prints text content
argv: -t p     stdin: page.html (blob ...)   channel: stdin
exit 0
stdout: |
  Hello world
control root-t0099: argv: p   exit 0
stdout: |
  <p>Hello <b>world</b></p>
```

The error unit is listed by template: template id, exit, template text, member count, argv and option set of every member (one per line; beyond 30
the rest go into an attached file); the default unit gives ≤20 deduplicated representatives;
the combinations unit gives a summary of the five tests of each IV relation. Each output is truncated to about 2KB, with the blob path attached.

### 6.3 Units and writing order

| # | Unit file | Source | What to write | Assert / verify |
|---|---|---|---|---|
| 1 | `errors.json` | `errors` class (pre-grouped by template) | agent groups by error cause into E01…: title, causes and their members | none |
| 2 | `exclusions.json` | II in relations | for each II: which two options, how the error looks | none |
| 3 | `default.json` | `default` class | prose description of the zero-option behaviour | none |
| 4 | `opt<name>.json` ×N (e.g. `opt--text.json`) | `option:A` class | Given / When / Then / Assert / Basis | **yes**: agent picks Asserts to verify |
| 5 | `combinations.json` | IV and I in relations | for each IV: who overrides whom; for each I: A only has an effect when B is present | none |
| 6 | `env-<VAR>.json` ×M | `env:X` class | values, default, precedence over options, behaviour description | none |
| 7 | `overview.json` | earlier units + help | purpose, Usage, Options list | none |

**Option unit schema**:

```json
{
  "option": "--text", "aliases": ["-t"],
  "blocks": [
    {"given": "HTML on stdin with nested elements",
     "when": "--text with a selector",
     "then": "prints the text content of each selected element instead of markup",
     "assert": [
       {"id": "A1", "text": "stdout is each selected element's text, one per line"},
       {"id": "A2", "text": "descendant text is included, tags removed"}
     ],
     "basis": ["root-t0123", "root-t0124"]}
  ],
  "verify": [
    {"assert": "A1", "test": "root-v0003", "new_aspect": "...",
     "verdict": "inconsistent", "reason": "...", "revised": true}
  ]
}
```

- Options without a witness: still write a block, with `then` based on help/docs and `basis: ["help"]`; Assert may be empty.
  The agent may also verify it. "Showing an effect" is decided exactly like a witness: the verify test exits 0 (or with one of nonerror_exits),
  the script automatically runs the control "without that option, everything else unchanged", both are stable and the signatures (exit/stdout/stderr/outfiles) differ.
  When this holds the test is recorded as a witness of the option and goes into basis and the differential suite.
- Errors are grouped by the agent by **error cause**: several templates may be merged into one (same cause, messages differing in non-argv parts), and one
  template may be split (same message, different causes). The members of each cause are given as template ids (the whole group) or test ids (single tests, which take precedence over template membership).

**Errors unit schema**:

```json
{"entries": [
  {"id": "E02", "title": "Missing required option value",
   "causes": [
     {"text": "a value-taking option is the last token", "members": ["T03"]},
     {"text": "a value-taking option is followed by another flag", "members": ["T04", "root-t0311"]}
   ]}
]}
```

Validation: every error test belongs to exactly one entry. When rendering, the script fills in Message (the most common template), Exit and
Minimal example (the shortest argv of each cause) from the members.

### 6.4 Falsification (option units only)

The aim is not to reproduce existing observations but to **try to falsify the Assert**: within the scope described by Given, build inputs that existing tests did not consider,
run the gold, and judge whether the actual result still matches the Assert's wording.

```
./pk verify <sub> --option --text --assert A1 --why "..." [condition arguments of ./pk t] -- ARGV...
```

- `--why`: **what is new** about this input compared with the tests in basis (e.g. "deep nesting + adjacent text nodes", "empty element", "non-ASCII text").
- The script checks that the input (combination of argv, files, stdin, env, channel) is not the same as any test in the entry's basis; then it runs the gold
  (recorded as `source=verify:--text:A1`) and prints the actual result, **with no mechanical comparison**.
- The agent compares it with the Assert text, judges `consistent` / `inconsistent`, and writes it into the `verify` field of the option unit:

```json
{"assert": "A1", "test": "root-v0003",
 "new_aspect": "adjacent text nodes split by an inline element",
 "verdict": "inconsistent",
 "reason": "text of <b> is joined without a newline, the Assert said one line per element"}
```

- **The agent decides which Asserts to verify**. The prompt gives guidance: general claims (any/always/all), conclusions resting on only 1 witness,
  format rules induced from samples (field order/delimiters/whitespace/newlines), boundary-related claims; Asserts that merely restate existing tests need no verification.
- **Each Assert is verified at most once**; when inconsistent the entry is revised once, the revision must agree with this test, and the test is added to basis;
  no re-verification, **no unverified marker**.
- Verify tests go into the differential suite, attached to that option entry.

### 6.5 Rendering

`./pk spec build <sub>`: validate all units (coverage: every error class, every option, every env, every relation has an entry;
referenced test ids exist; verify rules satisfied) → render `SPEC.md` → write `entry_index.json`.

SPEC.md is rendered by `lib/specbuild.py` with the following structure (error entries list all message variants, and each cause gives one shortest example command):

```markdown
# executable <command words>
## 1. Overview and interface
## 2. Rejection contract
### Errors            (#### Error E01: ... / Cause / Message variant k / Minimal example(s))
### Cannot be combined
## 3. Behaviour
### Default behaviour — zero options
### Option behaviour  (#### --name (aliases) / Given / When / Then / Assert / Basis)
### Combinations
### Environment
```

`entry_index.json`: `{"root-t0123": ["root:option:--text"], "root-t0050": ["root:errors:E02"], ...}` —
the mapping test → class → SPEC entry, used by step 4 to group failures.

---

## 7. Step 4: implementation and differential testing

### 7.1 Agent context

| Content | How |
|---|---|
| Task prompt (see 7.4), delivery requirements | prompt |
| Paths of all `SPEC.md` files (one per subcommand) + subcommands.json | prompt, must be read first |
| Raw help, original docs | paths |
| gold | may be run; `./pk t <sub> --tag impl` recommended (not added to the suite) |
| `./pk diff` reference | prompt |

The full test set is not put into the context; it is only viewed on demand through the `./pk diff` summary.

### 7.2 Differential testing

Suite = `suite.json` of all subs ∪ step 3 verify tests, frozen at the end of step 3.

```
./pk diff                       build (run impl/compile.sh in a clean copy) and run all tests
./pk diff --entry root:option:--text   failure details of that entry (argv of each test and per-stream diff)
./pk diff --test root-t0123     full gold/candidate comparison of a single test
```

Comparison uses the frozen gold observations (the gold is not re-run), under the same isolation and conditions, byte by byte on exit, stdout,
stderr and written files (`@PROBE_DIR@` normalised). The summary is grouped by `entry_index.json`:

```
build ok   pass 812/870 (93.3%)   Δ vs previous: +14 / -2 (regressions: root-t0311, root-t0312)
root:errors:E02             3/11 fail   e.g. root-t0050 stderr differs at byte 41
root:option:--pretty        9/14 fail   e.g. root-t0201 stdout line 3 indentation
root:default                1/20 fail
```

### 7.3 Amending the SPEC

The agent judges whether a failure is a spec error or a code error. For a spec error: edit the corresponding `sections/<unit>.json` and then run

```
./pk spec amend <sub> <unit> --cite <test_id> --reason "..."
```

This checks that the cited test exists and is a stable gold observation → re-renders SPEC.md → appends to `spec_changes.jsonl`. Changing the SPEC to accommodate the candidate implementation is not allowed.

### 7.4 Prompt outline (draft)

```text
SPEC files are a record of observed gold behaviour, NOT the complete feature set.
- Behaviour the SPEC states: reproduce exit code, stdout and stderr byte for byte.
- Features named in help/docs but missing from the SPEC: implement them. Check the gold first with
  `./pk t <sub> --tag impl -- ...` instead of guessing.
- Capabilities that belong to this kind of program but appear in neither (full CSS selector grammar,
  gzip/zlib format details, regex dialect, operator precedence, ...): implement the standard semantics
  completely. Do not omit or reject them just because the SPEC has no example.
- Forbidden: hard-coding outputs keyed on test argv or inputs, lookup tables of recorded outputs,
  special cases written only to pass one differential test. The differential suite is a sample;
  the program will be graded on unseen tests.
- Before finishing: list every option, subcommand and documented feature from help/docs and confirm
  each is implemented and was compared with the gold at least once.
Loop: write code → ./pk diff → inspect the worst entries → fix code (or fix a wrong SPEC entry with
./pk spec amend, citing a gold test) → ./pk diff.
```

### 7.5 Delivery

The agent writes the source and `compile.sh` (producing `work/impl/executable`) in `work/impl/`. Afterwards the driver calls
`./pk export /workspace`: copies `work/impl/` to `/workspace/impl/` (to avoid name clashes with the accompanying docs), generates
`/workspace/compile.sh` (builds impl/ and installs a `./executable` launcher pointing to `impl/executable`), writes `.gitignore`, makes a git commit, packs a tar in the official format into
`work/submission/<instance_id>/submission.tar.gz`, and test-compiles it in a temporary directory.

---

## 8. Driver (`driver/drive4.py`)

- Docker mode: the container is started with the run_args/env of the official programbench.yaml, the workspace is mounted into the container,
  gold = `/workspace/executable`; model calls stay on the host.
- Order: step 1 → step 2 → step 3 → step 4 → export. Within steps 2 and 3 the agents of the subcommands **run in parallel**,
  with at most `--parallel N` (**default 5**) agents at once, starting the next whenever a slot is free; the next step starts only after the whole step (including retries) has finished.
- After each agent ends the driver runs that step's completion check (`./pk check 1` / `./pk finish` state / `./pk spec build` state /
  `./pk diff` builds).
- **Failure retry**: when an agent ends abnormally (API error, repeated format errors, context too long, early submission) and the completion check fails, the driver
  **starts a new agent for the same step and the same sub, at most once**. Recorded tests and written files stay on disk, and the new agent's prompt
  additionally says "the previous agent did not finish; the existing artefacts are as follows". If it still fails the sub is marked as failed for that step: step 2 failure → skip that sub's
  step 3; step 3 failure → that sub has no SPEC in step 4, only help and gold.
- **Step 4 repair rounds** (2026-10-04): after the step 4 agent ends, if the last self-test (`./pk diff`) still has failures and this round improved over the previous one
  (the first round always counts as an improvement), the driver starts another step 4 repair agent (prompt `prompts/step4_repair.md`, with the current diff summary,
  only fixes the remaining failures and may not trade regressions for passes), for at most `--repair-rounds` (default 2) rounds; it stops when everything passes or there is no improvement. The step 4 prompt
  also requires: do not submit while the self-test still fails, unless every failing entry has been attempted and the reasons are written into `work/impl/NOTES.md`.
  Reason: in a 9-instance batch, the step 4 agents of elfcat (main path 18/18 failing), revive and dep-tree all submitted with failing self-tests;
  step 3's falsification checks all passed on these instances and cannot catch implementation errors, so the repair goes into step 4.
- Each agent has its own scratch directory; there is no step limit and the agent decides how much to explore (`config4.yaml` only has a per-step
  wall-clock safety limit to stop stuck agents).
- Run records live in `mini_runs/`: `journal.jsonl` (start/end/number of calls/cost of each agent, each completion check),
  `NNN_<step>_<slug>.task.md` (the prompt actually sent), `.traj.json` (trajectory), `driver_state.json` (failed subs, how far the run got,
  read when resuming with `--from-step`). Check progress with `./pk status`.
- `--fake-agent tests/fake`: runs the four steps with scripts instead of a model, to check the driver, container and export flow (no model cost).
- Launch: `driver/launch4.sh INSTANCE_ID MODEL [drive4.py arguments]`, e.g.
  `driver/launch4.sh mgdm__htmlq.6e31bc8 openai/gpt-6-sol --model-class litellm_response`.

---

## 9. `./pk` command list

| Command | Step | Purpose |
|---|---|---|
| `discover <binary>` | 1 (driver) | capture the help tree, staging, copy docs |
| `check 1` | 1 | validate step 1 artefacts |
| `iface draft <sub>` | 2 (driver) | pre-fill interface.draft.json from help |
| `iface check <sub>` | 2 | validate interface.json + regex check against help |
| `t <sub> [conditions] -- ARGV` | 2/3/4 | run and record one test (program arguments must come after a separate `--`) |
| `rules <sub>` | 2 | pick a base and run the 2.6 rule tests (called automatically by the first coverage; runs only once) |
| `coverage <sub>` | 2 | provisional clustering + controls + witnesses + gap report |
| `finish <sub>` | 2 | validation + final clustering and filtering + freeze |
| `spec prepare <sub>` | 3 (driver) | generate spec_input/ evidence files |
| `verify <sub> ...` | 3 | falsification: check the input is new, run gold, record |
| `spec build <sub>` | 3 | validate units + render SPEC.md + entry_index |
| `diff [--entry E | --test T]` | 4 | differential testing |
| `spec amend <sub> <unit> --cite T` | 4 | amend SPEC |
| `export DEST` | 4 (driver) | export submission |
| `status` | throughout | progress board |

---

## 10. Implementation plan

1. `lib/execute.py` + `testdb.py` + `pk t`: verify isolation, channels, double run and normalisation on a mock binary.
2. `iface.py` + `argvparse.py` + `pk iface`: pre-fill and parsing tests with the help of htmlq, xsv and zoxide.
3. `rules.py` + `pk rules`: check the generated cases and their results on the mock binary and htmlq.
4. `cluster.py` + `pk coverage/finish`: verify grouping, controls, witnesses and filtering with a constructed test database.
5. `specbuild.py` + `verify.py` + templates and one-shots.
6. `difftest.py` + `pk diff/spec amend` + `export.py`.
7. `prompts/` + `driver/drive4.py`.
8. End-to-end trial runs on htmlq / eva / pigz, then extend to revive / ditaa / jplot etc.

---

## 11. Open issues

(none yet)


---

## 12. code5: coverage gate for step 2 (2026-10-04)

Motivation: about 80% of code4's failures on pls / melody / eva were "not covered by exploration" — the step 2 agent called
`pk finish` after only 12–26 calls, and finish only checked that coverage had run, FINDINGS and relations; coverage of input languages/formats was not constrained at all.
code5 turns the coverage requirement from prompt text into a script-enforced rule, with four components sharing one checklist.

### 12.1 Checklist (work/<sub>/features.json + features_auto.json)

| Field | Values |
|---|---|
| kind | construct (syntax construct, function, keyword, symbol, record kind, file type…) / error (one error situation) / vocab (a closed set of names) |
| source | doc / prior (the agent's prior knowledge of the language or format; a complete feature table is required) / error (error matrix) / review (gap-filling agent) / leak (script extraction) / sweep (words accepted by a sweep) |

- The agent writes features.json; the script writes features_auto.json (leak, sweep). Items in features.json with the same id as an automatic entry
  are only used to supply `tests` and `unreachable`.
- **Leak extraction** (lib/features.py `extract`; scans the stdout/stderr of new tests after each `pk t`, and all tests and help at coverage/ready/finish):
  - `expected A, B, or C` (≥2 identifiers or containing underscores) → one item `rule:A` per name (construct; the original error fragment is recorded,
    and a covering test whose output still contains that fragment does not count as success);
  - `[possible values: a, b]` → `value:--opt=a` (when the option can be located, any successful test passing that value to that option covers it automatically);
  - `did you mean 'x'` → `word:x`;
  - `unknown/unrecognized/undefined/unsupported/invalid/no such <noun> ['"<(or end of line]` → `vocab:<noun>`
    (excluding command-line-parser nouns and open-set nouns such as argument/option/file/value).
- **Status is decided by the script**: a construct with an associated successful test = covered; a vocab with a `pk sweep --covers ID` run =
  covered; an error with any associated test = covered; construct/vocab may also be `unreachable`: give a reason + ≥2 associated
  failing tests with mutually different inputs (threshold 2, set by the user on 10-04). Association: `pk t --covers ID`, the entry's `tests` list,
  value entries matched automatically by option value.
- `pk ready` / `pk finish` refuse while there are open entries and list them one by one; `pk coverage` appends the checklist gaps at the end of the report;
  `pk features SUB` shows the full table.

### 12.2 Error matrix

Written in step2 prompt 2.L: unbalanced brackets/quotes, empty element `f(1,,2)`, identifier immediately followed by digits or underscore, multiple decimal points, overflow,
out of domain, division/modulo by zero, unterminated strings and comments, unknown names, adjacent operators, a valid prefix followed by garbage; data formats additionally have truncation,
unterminated values, trailing commas, invalid escapes, type errors, invalid UTF-8. The agent writes the applicable ones as kind=error entries (covered once there is a test).

### 12.3 Dictionary scan `pk sweep`

```
./pk sweep SUB --list NAME [--list-file F] --covers vocab:X [same input options as pk t] -- ARGS (containing {W})
```

- {W} may appear in arguments, `--stdin-text`, `--env` values, file names and fixture file contents; one test per word (double run as usual),
  source=`sweep:<id>`, recording `sweep: {run, word}`. At most 600 words per run.
- Generic word lists in templates/wordlists/ (only public standards and common conventions): math_functions, symbol_names,
  unicode_categories (long names, lower snake case, short names), repl_keywords, lang_keywords, filenames; the agent may write its own lists.
- The summary groups by (exit code, stdout/stderr with the word replaced by {W}): lists all accepted words with the first line of their output, the error of the rejected majority,
  and the words that "reacted differently"; when all are accepted with identical output, or all are rejected, it warns that the template may be wrong. Accepted words automatically become
  covered entries `vocab:X/word`.
- Retention rules (cluster.py, set by the user on 10-04): all accepted words go into the SPEC and the differential suite; the rejected majority keeps only 2 representatives;
  other reactions keep at most 3 per group. Sweep tests form their own cluster (kind=sweep) and do not take part in option/env witnesses.

### 12.4 Gap-filling agent (step2_review, before freezing)

```
step2 exploration agent: explore → pk coverage → fill gaps and checklist → pk ready (all gates pass, writes ready.json, does not freeze)
step2_review: new agent, reads help/docs/interface/FINDINGS/checklist/`pk tests` summary → adds tests and entries (source=review),
              spot-checks covered and unreachable → appends ## Review to FINDINGS → pk finish (passes the gates again and freezes)
```

Driver: the exploration agent's check is `pk check 2r`, the gap-filling agent's check is `pk check 2`; each retries once on failure; if the gap-filling agent
fails both times the driver runs `pk finish` itself (the gates still apply). `pk finish` requires ready.json to exist.

### 12.5 The features unit in step 3

At freeze time a snapshot of the checklist is written into clusters.json (`features`, `sweeps`), and all associated tests (except rejected words dropped by sweep) are forcibly
kept in the SPEC and the differential suite, unaffected by option/combo filtering. Step 3 gets a new unit `features` (sections/features.json:
`{"entries": [{"ids": [...], "title", "text"}]}`); every covered/unreachable entry (sweep words are merged into their vocab)
must appear in the ids of some entry; the SPEC renders it as "### Input features", and the associated tests are mapped to
`feature:<id>` in entry_index.

## 13. code6: batch probing with `pk batch` (2026-10-04)

Motivation: melody × gpt-5.6-sol (code5) failed 473 tests, the official single agent (same model) 272. Almost all of the gap is in exploration: ranges were tried in only
2 spellings before being marked unreachable (threshold 2 treated as a cap), the symbol word list missed alphabetic/alphanumeric/backspace, and lazy/negated classes/comments
were not put on the checklist. The official agent writes a script per call that tries dozens of candidate spellings against the gold at once; with us each `./pk t` is one call, so guessing a
spelling is expensive and weak models guess less.

Mechanism:
- `./pk batch SUB --cands FILE [--cand TEXT]... --covers ID [--split] [input options of ./pk t] -- ARGS`, with placeholder
  `{C}` (allowed in arguments, --stdin-text, --env values, fixture file names and contents). The candidates file has one candidate per line; when it contains
  `---` on a line of its own it is split into blocks, allowing multi-line candidates. `--split`: an argument that is exactly `{C}` is split into several arguments by shell rules (to guess argument forms).
  At most 200 candidates per batch.
- The implementation reuses sweep: test id prefix `b`, source `batch:<ID>`, rec["sweep"] = {run: Bnn, word, batch: true},
  sweeps.json records kind=batch. Every candidate is `--covers ID`: accepted ones are successful tests of that item, failed ones count towards the unreachable attempts.
- Retention rules (SPEC and differential suite): all accepted ones are kept; rejected ones keep 1 representative per reaction, at most 6 in total (the errors for candidate spellings often quote
  the candidate itself, giving many reaction groups, and the sweep rules would keep too many).
- Gate: the number of distinct failed inputs needed for unreachable, MIN_FAILED, goes from 2 → 8 (one batch is enough, but giving up after two tries is no longer allowed).
- Prompts: step2 "Guess in bulk" + a 2.L subsection on guessing spellings in bulk (candidate sources: doc examples, rule names and expected lists in error messages,
  spellings of similar languages/tools); step2_review updated to match; when `./pk t --covers` fails it prints a hint to use batch instead.
- Also fixed: specbuild crashed when encoding error templates containing non-UTF-8 bytes (surrogates) (json-tui step 3 produced no SPEC).

### 13.1 Second round (2026-10-04 23:55)
First melody code6 run 809/1205: lazy was found; still missing were extended symbols (the agent used only its own 18-word list), character ranges (prior:range was taken as covered by quantifier ranges),
and negated classes (`"abc";` was treated as a character class). Changes:
- sweep automatically merges the framework word list by checklist item id (vocab:*symbol*/*class* → symbol_names, *categor* → unicode_categories,
  *function* → math_functions, *repl*/*command* → repl_keywords, *keyword* → lang_keywords, *file* → filenames).
- step2 2.L: when an input file is itself a small language, test it construct by construct as well; list the full set of standard constructs for regexes/regex builders; two constructs sharing one word
  (character range vs repetition range, character class vs literal string) are listed separately, and only a test whose output exhibits exactly that construct counts as covering it.
