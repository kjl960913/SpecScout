# Step 1 of 4: command inventory and documentation index

Workspace: {{workspace}} (run every command as `cd {{workspace}} && ...`)
Scratch directory for temporary files: {{scratch}}
{{retry_note}}
We are reverse-engineering the CLI program `executable` from its behaviour. Later agents explore each
command node separately, so this step must get the list of command nodes right.

The harness already ran `./pk discover`. Result:

```
{{discover_summary}}
```

Root help (work/help/root.txt):

```
{{root_help}}
```

Documentation files copied to work/docs/ (sizes in bytes):

```
{{docs_list}}
```

## Your job

1. Check the command list against the help texts (work/help/*.txt) and the documentation.
   - A subcommand mentioned in help or docs but missing above: add it with
     `./pk node add WORD [WORD...]` (full command path; it captures that command's help and tells you
     whether it worked). Check by its output that it really is a command of the program.
   - A node that is not a real command (a misparsed help line): `./pk node remove SLUG`.
   - Do not add `help` itself, and do not add aliases of a command that is already listed.
2. Write work/docs_index.json mapping every documentation file to the command nodes it is relevant to
   (use the slugs printed above, `root` for the program itself):
   `{"docs/README.md": {"size": 1234, "commands": ["root", "add"], "summary": "usage, env vars, examples"}}`
   work/docs_index.draft.json lists the files and sizes. Read docs with grep/sed/head; do not print huge files.
3. Run `./pk check 1` until it prints `ok`, then finish.
