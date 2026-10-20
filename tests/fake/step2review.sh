#!/bin/bash
# scripted step 2 review for SLUG: list the tests, add one review test, finish
set -e
S=$1
WORDS=$(python3 -c "import json;print(' '.join(next(n['path'] for n in json.load(open('work/subcommands.json'))['nodes'] if n['slug']=='$S')))")
./pk tests $S | tail -5
./pk t $S --tag review --stdin-text '<p>x</p>' --note "review: tiny document on stdin" -- $WORDS | head -3
printf '\n## Review\n- scripted review\n' >> work/$S/FINDINGS.md
./pk finish $S
