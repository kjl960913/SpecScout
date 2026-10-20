# Step 2 of 4: explore one command — `{{sub_display}}` (slug `{{sub}}`)

Workspace: {{workspace}} (run every command as `cd {{workspace}} && ...`)
Scratch directory for temporary files: {{scratch}}
Put input files for tests in work/fixtures/{{sub}}/ .
{{retry_note}}
We are reverse-engineering the CLI program `executable` from its behaviour. You explore ONE command;
other agents explore the other commands at the same time. Everything you observe is recorded as tests
that later become the specification and the differential test suite for a reimplementation.

## The one tool for running the program

```
./pk t {{sub}} [--file NAME[=SRC]]... [--dir NAME] [--stdin FILE | --stdin-text TEXT]
       [--env NAME=VALUE]... [--unset NAME] [--channel CHANNEL] [--setup 'ARGV']
       [--tag 2.1|2.2|2.3|2.4|2.5|lang|tui] [--covers ITEM]... [--note "what this test checks"] -- ARGS...
```

- ARGS are the program's arguments **including the command words** (`{{sub_words}}`), e.g.
  `./pk t {{sub}} --file in.txt --note "basic run" -- {{sub_words_example}}in.txt`.
- `--file NAME=SRC` copies work/fixtures/{{sub}}/SRC into the test's empty working directory as NAME;
  names starting with `~/` go into the test's private HOME.
- Every test runs in a fresh directory with a private HOME, twice (to detect nondeterminism), and is
  recorded. NEVER run work/bin/executable or /workspace/executable directly: unrecorded runs are lost.
- Give every test a `--note` and the `--tag` of the goal it serves; `--covers ITEM` links it to checklist
  items (goal 2.L).
- **Guess in bulk.** Whenever you do not know how something is written (a construct of an input language,
  a keyword, an option value, an argument form), do not try spellings one at a time: write 10-40
  candidates into a file and run them in one call with `./pk batch` (goal 2.L). It is as cheap as one test.

## Help text of this command (work/help/{{sub}}.txt)

```
{{help_text}}
```
{{root_help_block}}
## Documentation relevant to this command

{{docs_list}}
(Read them with grep/sed/head. Example commands in the docs are the best first tests.)

## Goals, in this order

1. **Interface.** Copy work/{{sub}}/interface.draft.json to work/{{sub}}/interface.json and correct it:
   every option from help AND docs with its aliases; `takes_value` (false / "required" / "optional");
   the positionals (`kind`: scalar, file or text); `env_vars` named in help or docs, with `source`;
   `nonerror_exits` (exit codes that mean "no match"/"differences found" rather than an error, e.g. [1]
   for grep-like programs); `uses_root_globals` (true when options of the root command may appear in
   this command's invocations). Mark an option `"context": true` only when ALL of these hold:
   (a) its only job is to say WHERE input is read from or output is written to (`-f/--file/--input
   FILE` instead of stdin or a positional file, `-o/--output FILE` instead of stdout, `-C/--directory
   DIR` to run elsewhere); (b) dropping it and supplying the same input another way (stdin or a
   positional) — or reading the same output from stdout — gives the same result; (c) its value is a
   location whose content is only data. Never mark options whose file content changes the processing
   (`--config`, `--rules`, `--dictionary` FILE) or any option that changes format, selection, sorting or
   encoding. When unsure, do not mark. Tests then group by their other options: `-f in.html -t p` counts
   as a test of `--text`, compared with `-f in.html p`. A context option counts as covered once it is
   used in a successful test; still test its error paths (missing, unreadable, unwritable locations).
   Run `./pk iface check {{sub}}` and fix what it reports. Keep the file up to date if you discover more
   options later.
2. **Explore.** Parser-level edge cases (number of positional arguments, empty arguments, option
   spellings such as aliases, `=` forms, upper-case, truncated, repeated options, missing option values,
   misspelled command words, unreadable/missing files, byte-level input boundaries) are generated
   automatically later — do not spend tests on them.
   2.1 Make every option's function visibly happen at least once: a successful run whose output
       differs from the same run without that option. Choose inputs that can reveal the effect (an
       option that ignores case needs mixed-case data, a sort option needs unsorted data, ...).
   2.2 For every environment variable named in help or docs, make its effect visible the same way:
       the same run with and without `--env NAME=VALUE`.
   2.3 Option pairs, 2-way only. Definitions, always within one identical context (same input files,
       stdin, environment, channel and positional arguments):
       - **I (A depends on B)**: A alone shows no effect (its output equals the run without it), B alone
         has an effect, and A+B succeeds with output different from B alone. Typical: a separator or
         format option that only matters when another option is on.
       - **II (mutually exclusive)**: A alone succeeds, B alone succeeds, but A+B and B+A both fail.
       - **IV (one dominates)**: A+B succeeds and its output equals the output of A alone (or of B alone).
       Combine options freely and decide yourself which pairs could plausibly be I, II or IV (help
       wording such as "requires", "only with", "used with", "overrides", "conflicts with", "ignored
       when", "implies"; several output-format or verbosity options; a value option given twice;
       --help/--version with other options; and every option that showed no effect alone). For each
       candidate run base, A, B, A+B, B+A on the same input and record confirmed pairs in
       work/{{sub}}/relations.json:
       `[{"a": "--sep", "b": "--number", "class": "I", "tests": {"base": "ID", "a": "ID", "b": "ID", "ab": "ID", "ba": "ID"}, "reason": "..."},
         {"a": "--json", "b": "--csv", "class": "II", "tests": {...}, "reason": "..."},
         {"a": "--quiet", "b": "--verbose", "class": "IV", "dominant": "--quiet", "tests": {...}, "reason": "..."}]`
       For I, `a` is the dependent option and `b` the option it depends on; the script checks the
       definition (`./pk coverage` reports problems) and then counts `a` as "triggered only with b".
       Write `[]` if you find none. Do NOT enumerate all pairs.
   2.4 Channels. The same invocation can behave differently when stdin carries data or when
       stdout/stderr is a terminal: `--channel` pipe (default; stdin is /dev/null) | stdin (stdin from
       --stdin/--stdin-text as a file) | stdin_pipe (stdin as a pipe) | tty_out (stdout and stderr are a
       terminal) | tty (stdin, stdout, stderr are a terminal; terminal runs stop after 2 s) | stdin_tty
       (stdin a pipe, output a terminal). Check a plain working invocation and the options likely to
       depend on it (colour, paging, progress, interactive mode, reading stdin when no file is given)
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
   2.L Languages and input formats (tag `--tag lang`). Two cases:
       (a) An argument or option value is itself a small language — a selector (CSS, XPath, JSONPath),
       an expression or arithmetic syntax, a regular expression, a format/template string, a query or
       filter language, a range/field list. Test its grammar construct by construct: every construct the
       help or docs mention, plus the standard constructs of that language (for CSS e.g. type/class/id/
       universal selectors, attribute selectors with each operator, descendant/child/sibling
       combinators, selector lists, pseudo-classes such as :first-child, :nth-child(an+b), :not(...)).
       The same holds when an input FILE is written in a small language (a regex builder, template,
       query or rule language, a DSL): its constructs are what the program is about. For a regular
       expression or anything that compiles to one, the standard constructs are: literals and escapes
       (\n \t \uXXXX, quoting styles), character sets [abc], negated sets [^abc], character ranges
       [a-z] and negated ranges [^0-9], predefined classes and their negations (\d \w \s \b, POSIX and
       unicode classes/categories \p{L}), anchors (start/end of line and input), groups, captures and
       named captures, alternation, quantifiers (exact, ranges, open ranges, optional) in greedy and lazy
       forms, backreferences, lookahead/lookbehind and their negations, flags/modifiers, comments,
       variables or reusable definitions if the language has them. A regex BUILDER spells each of these in
       its own words: find the spelling of every one (`./pk batch`). Each construct is its own checklist
       item; when two constructs share a word (a range of characters vs. a range of repetitions, a class
       of characters vs. a literal string), they are separate items, and an item counts as covered only
       by a test whose output shows exactly that construct (e.g. the compiled `[a-z]`, `[^abc]`).
       (b) The program parses a structured input format — JSON, YAML, TOML, CSV/TSV, XML/HTML, an INI or
       config file, source code of some language, a binary format such as ELF, gzip or images. Feed
       inputs that exercise each construct of that format the program handles (nesting, escapes, unicode,
       numbers, empty values, every record/section kind ...) and one malformed input per kind of error
       (truncated data, unclosed brackets/strings, trailing commas, bad escapes, wrong magic or version,
       invalid literals, wrong types ...). Error messages often quote positions and tokens; record each.
       Give each construct at least one successful test on input where it matters, and add malformed or
       unsupported forms to record their exact errors. These tests become the specification and the
       regression suite of the reimplementation, so a construct you never run will probably be
       implemented wrongly.

       **The checklist (work/{{sub}}/features.json) — enforced by `./pk ready`.** Write the input
       features of this command as a JSON list, and keep it up to date while you explore:
       `[{"id": "prior:lazy-quantifier", "kind": "construct", "source": "prior", "description": "..."}, ...]`
       - `kind`: `construct` (a syntax element, function, keyword, symbol, record kind, file type ...),
         `error` (one error case worth recording), `vocab` (a closed set of names: functions, symbols,
         categories, keywords, units, file names ...).
       - `source`: `doc` (help/docs mention it), `prior` (you know it from the language, format or
         program family — if you recognise the language or format, list its COMPLETE feature set from
         memory, not just what the docs show), `error` (from the error matrix below).
       - Items the script finds in the program's own output are added automatically (features_auto.json):
         grammar element names in parser messages (`expected literal, range, or group` → `rule:range`),
         `[possible values: ...]` (`value:...`), "did you mean 'x'" (`word:x`), and "unknown/unrecognized
         X 'y'" messages (`vocab:X`). `./pk t` prints them as NEW CHECKLIST ITEMS. To annotate one, add an
         entry with the same id to features.json (only `tests` and `unreachable` are read).
       - Link tests with `./pk t ... --covers ID` (or list ids in the item's `"tests"`).
       - `./pk ready` refuses while an item is open. A `construct` or `vocab` item is done with a linked
         SUCCESSFUL test (vocab: a sweep, below); if after real effort you cannot make it succeed, record
         at least 8 different failing attempts linked to it (one `./pk batch`, below) and add
         `"unreachable": "why"`. Leak items are hints from the program itself: a grammar element it names
         exists — keep trying other spellings in batches (the program's error messages and the documentation
         examples show the surrounding syntax). An `error` item is done with any linked test (its point is
         the exact message).
       - Error matrix: list as `error` items the cases that apply to this input, at least: unbalanced or
         extra brackets/parentheses/quotes, empty arguments or elements (`f(1,,2)`, `[1,,2]`), an
         identifier immediately followed by digits or `_`, several decimal points, numeric overflow and
         huge values, values outside a function's domain (negative roots/logarithms), division or modulo by
         zero, unterminated strings and comments, unknown names, two operators in a row, a valid prefix
         followed by garbage; for data formats: truncation, unclosed structures, trailing commas, bad
         escapes, wrong types, invalid UTF-8. Every distinct error prefix the program prints (e.g.
         `Syntax Error:`, `Parse error:`, `Domain Error:`) should have a test.
       - `./pk features {{sub}}` shows the checklist with each item's status. A command whose input has no
         language, format, name set or error cases at all writes `[]` (a review agent checks that claim).

       **Sweeps for closed sets of names.** When the input contains names from a closed set (functions,
       constants, symbols, character classes, categories, keywords, commands of a REPL, units, file names
       or extensions the program treats specially), probe the whole set with one command instead of
       guessing:
       ```
       ./pk sweep {{sub}} --list math_functions [--list-file my_words.txt] --covers vocab:function \
            [same input options as ./pk t; {W} is replaced by each word] -- ARGS with {W}
       ```
       {W} may appear in arguments, `--stdin-text`, `--env` values, file names (`--file '{W}=empty.txt'`)
       and inside fixture files (`--file prog.txt=template.txt` where template.txt contains {W}). Framework
       lists (templates/wordlists/): math_functions, symbol_names, unicode_categories, repl_keywords,
       lang_keywords, filenames; write your own list (one word per line, in your scratch directory or
       work/fixtures/{{sub}}/) for names you know from the language (a sweep of `vocab:symbol`,
       `vocab:function`, `vocab:category` ... automatically includes the matching framework list as well).
       First make one known-good name succeed
       in the template, then sweep. The script runs every word once, groups the reactions and prints them;
       accepted words become covered checklist items automatically. Inspect them with
       `./pk tests {{sub}} --sweep S01`, and test the accepted names you do not understand individually.
       **Batches for guessing spellings.** To find how a construct is written, run many candidate
       spellings at once instead of one `./pk t` each:
       ```
       ./pk batch {{sub}} --cands my_cands.txt [--cand TEXT]... --covers rule:range \
            [same input options as ./pk t; {C} is replaced by each candidate] -- ARGS with {C}
       ```
       my_cands.txt (scratch directory or work/fixtures/{{sub}}/) holds one candidate per line; for
       candidates spanning several lines separate them by lines consisting of `---`. {C} goes where the
       candidate belongs: inside a fixture file (`--file prog.txt=template.txt`, template.txt contains {C}
       and the known-good context around it), in `--stdin-text`, or in an argument (`-- -e '{C}'`; with
       `--split` an argument that is exactly `{C}` becomes the candidate's shell words, for guessing
       argument forms). Write 10-40 candidates per batch from: the documentation's examples and wording,
       the grammar element names and "expected ..." lists in the program's error messages, and the
       syntax of similar languages and tools you know (keywords, operators, brackets, quoting, word order,
       singular/plural, abbreviations). The script runs every candidate once, groups the reactions and
       prints them in a few lines; all candidates are linked to the item (accepted ones cover it, failed
       ones count as attempts), and every accepted candidate plus representatives of each rejection are
       kept for the SPEC. When none is accepted, read where and what the parser expected and write the next
       batch from that; an item becomes `unreachable` only after real batches failed. Use batches for
       every `prior` construct you are not sure how to spell, and for every leaked `rule:` item.
   2.T Interactive mode (tag `--tag tui`). Decide whether this command is interactive: it draws a
       full-screen or line-editing interface on a terminal, or waits for keystrokes (viewers, file
       managers, editors, REPLs, pagers, prompts). If so, set `"interactive": true` in interface.json and
       drive it with `./pk t {{sub}} --tui "KEYS" [--tui-size 80x24] [--tui-delay 0.5] -- ARGS`: it runs
       in tmux, captures the screen once at start and once after each key (`j`, `Enter`, `Up`, `C-c`,
       `Escape`, `text:abc` for literal text), and records whether the program is still running after
       the last key. Cover start-up screens, every documented key binding, navigation, modes, quitting,
       resizing-sensitive layout (try another --tui-size) and the screens shown for errors. If the
       command is not interactive, skip this item.
3. **Coverage.** When you have finished exploring, run `./pk coverage {{sub}}`. The first call also
   runs the automatic parser-level tests and summarises them. It lists every option or variable whose
   effect was never made visible (with the reason) and every open checklist item. Address the option
   gaps and anything surprising in the automatic tests in one more round of tests, and close every open
   checklist item (the automatic tests may add new ones).
4. **Findings.** Write work/{{sub}}/FINDINGS.md:
   ```
   ## Behaviour summary      (3-10 key points)
   ## Not triggered          (each remaining gap and why)
   ## Uncertain              (behaviour observed but not understood)
   ## Channels               (which channels change behaviour)
   ```
5. Run `./pk ready {{sub}}`. It checks everything above (relations, FINDINGS.md, every checklist item)
   and must succeed; then finish. A review agent continues after you and freezes the tests.
