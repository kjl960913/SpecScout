# Step 4, repair round {{round}} of {{max_round}}: fix the remaining differences

Workspace: {{workspace}} (run every command as `cd {{workspace}} && ...`)
Scratch directory for temporary files: {{scratch}}

We are reverse-engineering the CLI program `executable`. A previous agent wrote a reimplementation in
work/impl/ (build: work/impl/compile.sh -> work/impl/executable) and stopped while the differential
self-test still fails. You continue its work: keep what passes, fix what fails.

SPEC files of the commands:

{{spec_list}}

Help texts: work/help/*.txt. Original documentation: work/docs/ (index: work/docs_index.json).
Notes left by earlier agents, if any: work/impl/NOTES.md.

Current self-test result (`./pk diff`):

```
{{diff_summary}}
```

## Your job

1. Read the code in work/impl/ and the SPEC entries that fail.
2. For each failing entry, look at the exact differences: `./pk diff --entry ENTRY` and
   `./pk diff --test ID` show the original's recorded output next to yours. Fix the cause in the code
   (it usually affects many hidden tests beyond the recorded ones), then run `./pk diff` again.
   Watch the "regressions" line: never trade passing tests for failing ones.
3. When an output is long or byte-exact (reports, tables, generated files), reproduce it from the
   original's recorded bytes, not from the SPEC prose. Check further inputs against the original with
   `./pk t SLUG --tag impl [...] -- ARGS` (add `--tui "KEYS"` for interactive commands).
4. If the recorded original output contradicts the SPEC, correct the unit JSON and run
   `./pk spec amend SLUG UNIT --cite TEST_ID --reason "..."` — never change the SPEC to match your program.

Robustness: the program must never crash (traceback, panic, unhandled exception) when stdout/stderr are
pipes or files, stdin is empty or a pipe, there is no terminal, or TERM/COLUMNS/LINES are unset — give every
terminal query a fallback and, in those environments, do what the original does (check with
`./pk t SLUG --tag impl --channel pipe -- ARGS`).

Rules: the code must never run, read, copy or embed the original binary; no hard-coded outputs keyed on
test arguments or inputs; implement general behaviour. compile.sh must keep working on a fresh copy.

Finish when `./pk diff` passes completely, or when every remaining failing entry has been attempted and
you have written in work/impl/NOTES.md why it is still failing. The last `./pk diff` must build.
