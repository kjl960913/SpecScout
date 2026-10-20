# Step 4 of 4: implement the program

Workspace: {{workspace}} (run every command as `cd {{workspace}} && ...`)
Scratch directory for temporary files: {{scratch}}
{{retry_note}}
We are reverse-engineering the CLI program `executable`. Earlier steps explored it and wrote one SPEC
per command:

{{spec_list}}

Help texts: work/help/*.txt. Original documentation: work/docs/ (index: work/docs_index.json).

## Your job

Write a complete reimplementation in work/impl/ in any language available here:
- work/impl/compile.sh (run with bash from work/impl/) must build `work/impl/executable` from your
  sources. It must work on a fresh copy of work/impl/ without network access.
- The program is invoked as `executable` with the same arguments as the original.
- Your code must never run, read, copy or embed the original binary.

Read every SPEC first. The SPEC files are a record of observed behaviour, NOT the complete feature set:
- Behaviour the SPEC states: reproduce exit status, stdout and stderr byte for byte.
- Interactive commands (tests on channel `tui`, screens captured in tmux after each key) are compared
  screen by screen; check the original with `./pk t SLUG --tag impl --tui "KEYS" -- ARGS`.
- Features named in help or docs but missing from the SPEC: implement them. Check the original first
  with `./pk t SLUG --tag impl [--file NAME=SRC] -- ARGS...` (same options as in step 2; input files
  from work/fixtures/SLUG/ or an absolute path) instead of guessing.
- Capabilities that belong to this kind of program but appear in neither (the full grammar of a
  selector or expression language, a file format standard, a regex dialect, operator precedence, ...):
  implement the standard semantics completely. Do not omit or reject them because the SPEC has no example.
- Robustness in every environment: hidden tests also run the program where the recorded suite rarely
  looks — stdout and stderr redirected to pipes or files, stdin empty, closed or a pipe, no terminal at
  all, any terminal size, TERM/HOME/COLUMNS/LINES unset. The program must never crash there (no
  traceback, panic or unhandled exception): every terminal query (size, attributes, tty checks) needs a
  fallback such as $COLUMNS/$LINES or 80x24. In such an environment do what the original does — it may
  render anyway, print plain output, print an error, or keep running until killed (tests recorded as
  timeouts, which the differential suite skips); check it with `./pk t SLUG --tag impl --channel pipe`
  (and the other channels) before deciding.
- Forbidden: hard-coding outputs keyed on test arguments or inputs, lookup tables of recorded outputs,
  special cases written only to pass one test. The differential suite is a sample; the program will be
  graded on many unseen tests.

## Loop

```
./pk diff                     build (compile.sh on a fresh copy) and run the whole recorded suite
./pk diff --entry ENTRY       failing tests of one SPEC entry, with diffs (ENTRY as printed, e.g. root:option:--width)
./pk diff --test ID           one test in full
```

Fix the worst entries first. If a failure shows that the SPEC itself is wrong (the recorded original
output contradicts it), correct the unit JSON in work/SLUG/sections/ and run
`./pk spec amend SLUG UNIT --cite TEST_ID --reason "..."` — never change the SPEC to match your program.

Do not finish while `./pk diff` still shows failures, unless you have tried to fix every remaining
failing entry; write what still fails and why into work/impl/NOTES.md. Before finishing: list every
option, command and documented feature from the help texts and docs, confirm each is implemented and
was compared with the original at least once, and make sure the last `./pk diff` built successfully.
Then finish.
