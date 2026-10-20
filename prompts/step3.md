# Step 3 of 4: write the SPEC of `{{sub_display}}` (slug `{{sub}}`)

Workspace: {{workspace}} (run every command as `cd {{workspace}} && ...`)
Scratch directory for temporary files: {{scratch}}
{{retry_note}}
We are reverse-engineering the CLI program `executable`. Step 2 explored this command and recorded
tests. A later agent will implement the program from your SPEC (it can also run the program), and the
recorded tests become the differential test suite. Write a SPEC that is precise about observable bytes:
output layout, whitespace, trailing newlines, exit status, stdout vs stderr.

## Inputs

Help text (work/help/{{sub}}.txt):

```
{{help_text}}
```
{{root_help_block}}
work/{{sub}}/interface.json:

```
{{interface}}
```

work/{{sub}}/FINDINGS.md (written by the explorer):

```
{{findings}}
```

Relevant documentation: {{docs_list}}

## Units to write, in this order

For each unit read its evidence file and write the JSON file (paths relative to work/{{sub}}/):

{{units}}

Formats: one example per unit kind in templates/examples/ (errors.json, exclusions.json, default.json,
features.json, option.json, combinations.json, env.json, overview.json). The examples describe a different program —
copy their shape, never their facts.

- **errors**: group the failing tests by CAUSE. A cause's `members` are template ids (T01: the whole
  group) or single failing test ids (they override their template). Merge templates with the same cause;
  split a template whose tests have different causes. Every failing test must belong to exactly one
  entry. Messages, exit status and examples are filled in from the members when the SPEC is rendered.
- **exclusions**: one entry per II relation. **combinations**: one entry per IV relation (with
  `dominant`) and per I relation (`a` acts only together with `b`), as listed in the evidence.
- **default**: behaviour without options: input interpretation, output format, edge cases. For an
  interactive command (interface.json `"interactive": true`, tests on channel `tui`) describe the
  screens and the effect of every key observed in the captures (`--- screen N ---` blocks).
- **features** (when present): the checklist of input features from step 2 — language constructs,
  names found by sweeps, error cases. Every listed item id must appear in the `ids` of an entry; one entry
  may describe several related items. For each: syntax, meaning, exact output bytes, and for vocab items
  the complete list of accepted names; for unreachable items what was tried and how the program answered.
- **option units**: one or more blocks `given / when / then`, `assert` (concrete, checkable statements)
  and `basis` (the test ids the block is based on; cite witnesses). An option without witness gets a
  block from help/docs with basis ["help"] or ["docs"]. A context option (interface.json
  `"context": true`) gets a block describing what it reads or writes and how, citing tests that use it.
- **env units**: values, default, precedence over options, behaviour text.
- **overview** (last): purpose, Usage, every option in one line.

Check a unit at any time with `./pk spec check {{sub}} --unit UNIT`.

## Falsification (option units only)

After writing an option unit, choose the asserts worth testing — general claims ("always", "any",
"every"), conclusions resting on one witness, format rules inferred from examples (ordering,
separators, whitespace, newlines), boundary claims. Skip asserts that only restate an existing test.
For each chosen assert, construct an input INSIDE the block's `given` that the basis did not consider,
and run it:

```
./pk verify {{sub}} --option NAME --assert A1 --why "what is new about this input" [--file ...] -- ARGS...
```

(same input options as `./pk t`; NAME is the canonical option name from interface.json). Judge whether
the actual result still agrees with the assert and record it in the unit's `verify` list:
`{"assert": "A1", "test": "<id>", "new_aspect": "...", "verdict": "consistent" | "inconsistent", "reason": "..."}`.
Each assert can be verified once. If inconsistent, revise that assert/block ONCE so it agrees with the
new test, add the test id to that block's basis and set `"revised": true`. Do not verify again.

## Finish

Run `./pk spec build {{sub}}`; it validates all units and renders work/{{sub}}/SPEC.md. Fix every
reported problem until it succeeds, then finish.
