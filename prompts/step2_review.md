# Step 2 review: find what the exploration of `{{sub_display}}` (slug `{{sub}}`) missed

Workspace: {{workspace}} (run every command as `cd {{workspace}} && ...`)
Scratch directory for temporary files: {{scratch}}
Put input files for tests in work/fixtures/{{sub}}/ .
{{retry_note}}
We are reverse-engineering the CLI program `executable` from its behaviour. Another agent explored this
command and handed over with `./pk ready`. Its tests become the specification and the differential
test suite of a reimplementation: behaviour nobody ran will be implemented by guessing. Experience shows
explorers stop early — typically after testing what the documentation shows, while the hidden tests
exercise the language constructs, names and error cases the documentation never mentions. You are a
fresh pair of eyes: find and record what is missing, then freeze.

## Material

- Help text: work/help/{{sub}}.txt (below). Documentation: {{docs_list}}
- work/{{sub}}/interface.json, work/{{sub}}/FINDINGS.md (the explorer's summary and open questions),
  work/{{sub}}/relations.json
- The checklist: `./pk features {{sub}}` (work/{{sub}}/features.json written by the explorer,
  work/{{sub}}/features_auto.json found by the script, sweeps in work/{{sub}}/sweeps.json)
- Every recorded test, one line each: `./pk tests {{sub}} > {{scratch}}/tests.txt` (redirect: it can be
  long; `--all` adds sweep word tests). Full record of one test: `grep '"id": "ID"' work/{{sub}}/tests.jsonl`
  and its output bytes in work/blobs/<first 2 chars of sha>/<sha>.
- Coverage of options and environment variables: `./pk coverage {{sub}}`.

```
{{help_text}}
```

## What to do

1. Read FINDINGS.md (especially "Uncertain" and "Not triggered") and the test list. Compare with the help
   text, the documentation and what you know about this kind of program, its input language or format.
2. Look for, and test (`./pk t {{sub}} --tag review --covers ITEM ... -- ARGS`, same options as in step 2):
   - documentation examples that were never run;
   - constructs of the input language/format missing from the checklist (if you recognise the language or
     format, compare the checklist against its complete feature set); add them as items with
     `"source": "review"`;
   - names from closed sets (functions, symbols, categories, keywords, REPL commands, file names ...) that
     were never swept: use `./pk sweep` (see `./pk --help`; {W} is the placeholder);
   - error cases from the error matrix (unbalanced brackets, empty elements, identifier+digits, several
     decimal points, overflow, domain errors, division by zero, unterminated strings/comments, unknown
     names, operator sequences, trailing garbage; for data formats truncation, unclosed structures,
     trailing commas, bad escapes, wrong types) without a test;
   - combinations of constructs that may interact (a construct inside each kind of container, a modifier
     on each kind of operand, comments or whitespace in each position, the first line vs later lines);
   - behaviour that depends on where the input comes from (file vs stdin vs argument) or on the channel.
   To find how something is written, guess in bulk: `./pk batch {{sub}} --cands FILE --covers ITEM
   [options as ./pk t, {C} where the candidate goes] -- ARGS` runs 10-40 candidate spellings (one per line,
   or blocks separated by `---` lines) in one call.
3. Check the checklist itself: does each covered item's linked test really exercise that item? Is each
   `"unreachable"` reason convincing — would another spelling or context make it succeed? If not, try
   another batch of spellings (from the parser's "expected ..." messages and similar languages).
   A `[]` checklist is acceptable only if the command really has no structured input.
4. Append what you learned to FINDINGS.md (a section `## Review`), keeping features.json valid.
5. Run `./pk finish {{sub}}`. It re-checks everything (every checklist item, including yours, must be
   covered or unreachable) and freezes the tests. It must succeed; then finish.

Do not delete or rewrite the explorer's tests or items; add to them. Do not edit interface.json unless an
option is missing or wrong.
