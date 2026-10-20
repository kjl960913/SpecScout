#!/usr/bin/env python3
"""A small CLI used by the framework tests: line filter with options, env, II/IV pairs and a subcommand."""
import os
import sys

HELP = """mockq 1.2.0 - print input lines with transformations

Usage: executable [OPTIONS] [FILE]...
       executable <COMMAND>

Commands:
  count   Count lines or words

Options:
  -u, --upper          Uppercase every line
  -l, --lower          Lowercase every line (wins over --upper)
  -n, --number         Number the lines
  -q, --quiet          Print nothing, only the exit status
  -s, --sep <SEP>      Separator between number and line [default: ": "]
  -o, --output <FILE>  Write the result to FILE instead of standard output
  -e, --eval <EXPR>    Evaluate an expression and print the result
  -h, --help           Print help
  -V, --version        Print version

Environment:
  MOCKQ_PREFIX         Text prepended to every line [env: MOCKQ_PREFIX=]
"""
COUNT_HELP = """Count lines or words

Usage: executable count [OPTIONS] [FILE]...

Options:
  -w, --words   Count words instead of lines
  -h, --help    Print help
"""


def err(msg, code=2):
    sys.stderr.write(msg)
    sys.exit(code)


def read_inputs(files):
    if not files:
        return [sys.stdin.read()]
    out = []
    for f in files:
        if f == "-":
            out.append(sys.stdin.read())
            continue
        try:
            with open(f) as fh:
                out.append(fh.read())
        except FileNotFoundError:
            err(f"executable: {f}: No such file or directory\n", 1)
        except IsADirectoryError:
            err(f"executable: {f}: Is a directory\n", 1)
        except PermissionError:
            err(f"executable: {f}: Permission denied\n", 1)
    return out


def count(args):
    words, files = False, []
    for a in args:
        if a in ("-h", "--help"):
            sys.stdout.write(COUNT_HELP)
            return 0
        if a in ("-w", "--words"):
            words = True
        elif a.startswith("-") and a != "-":
            err(f"error: unexpected argument '{a}' found\n\nUsage: executable count [OPTIONS] [FILE]...\n")
        else:
            files.append(a)
    text = "".join(read_inputs(files))
    print(len(text.split()) if words else len(text.splitlines()))
    return 0


FUNCS = {"double": lambda x: 2 * x, "neg": lambda x: -x, "sq": lambda x: x * x}


class ParseError(Exception):
    pass


def evaluate(src):
    """EXPR := number | name '(' EXPR ')' | '(' EXPR ')'  (a pest-like error names the grammar rules)"""
    pos = [0]

    def expect():
        raise ParseError(f" --> 1:{pos[0] + 1}\n  |\n1 | {src}\n  |\n  = expected number, function_call, or group")

    def expr():
        s = src[pos[0]:]
        if s[:1].isdigit():
            j = pos[0]
            while j < len(src) and src[j].isdigit():
                j += 1
            v = int(src[pos[0]:j])
            pos[0] = j
            return v
        if s[:1] == "(":
            pos[0] += 1
            v = expr()
            if src[pos[0]:pos[0] + 1] != ")":
                expect()
            pos[0] += 1
            return v
        if s[:1].isalpha():
            j = pos[0]
            while j < len(src) and src[j].isalnum():
                j += 1
            name = src[pos[0]:j]
            pos[0] = j
            if src[pos[0]:pos[0] + 1] != "(":
                expect()
            pos[0] += 1
            v = expr()
            if src[pos[0]:pos[0] + 1] != ")":
                expect()
            pos[0] += 1
            if name not in FUNCS:
                sys.stderr.write(f"error: unknown function '{name}'\n")
                sys.exit(1)
            return FUNCS[name](v)
        expect()

    v = expr()
    if pos[0] != len(src):
        expect()
    return v


def pager():
    """Interactive mode: j shows the next page, q quits (exit 0)."""
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    page = 1
    try:
        tty.setcbreak(fd)
        while True:
            sys.stdout.write(f"\x1b[2J\x1b[Hpage {page}\n")
            sys.stdout.flush()
            c = sys.stdin.read(1)
            if c == "q" or not c:
                return 0
            if c == "j":
                page += 1
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main(argv):
    if argv == ["--pager"]:
        return pager()
    if argv and argv[0] == "count":
        return count(argv[1:])
    opts, files, sep, i, out = set(), [], ": ", 0, None
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            sys.stdout.write(HELP)
            return 0
        if a in ("-V", "--version"):
            print("mockq 1.2.0")
            return 0
        if a in ("-e", "--eval"):
            if i + 1 >= len(argv):
                err("error: a value is required for '--eval <EXPR>' but none was supplied\n")
            try:
                print(evaluate(argv[i + 1]))
            except ParseError as e:
                err(f"error: parse failure\n{e}\n", 1)
            return 0
        if a == "--crash":
            err(f"thread 'main' ({os.getpid()}) panicked at src/main.rs:12:5\n", 101)
        if a in ("-u", "--upper"):
            opts.add("upper")
        elif a in ("-l", "--lower"):
            opts.add("lower")
        elif a in ("-n", "--number"):
            opts.add("number")
        elif a in ("-q", "--quiet"):
            opts.add("quiet")
        elif a in ("-s", "--sep") or a.startswith("--sep="):
            if a.startswith("--sep="):
                sep = a[6:]
            else:
                i += 1
                if i >= len(argv):
                    err("error: a value is required for '--sep <SEP>' but none was supplied\n")
                sep = argv[i]
        elif a in ("-o", "--output"):
            i += 1
            if i >= len(argv):
                err("error: a value is required for '--output <FILE>' but none was supplied\n")
            out = argv[i]
        elif a == "--":
            files += argv[i + 1:]
            break
        elif a.startswith("-") and a != "-":
            err(f"error: unexpected argument '{a}' found\n\nUsage: executable [OPTIONS] [FILE]...\n")
        else:
            files.append(a)
        i += 1
    if "quiet" in opts and "number" in opts:
        err("error: the argument '--quiet' cannot be used with '--number'\n")
    lines = "".join(read_inputs(files)).splitlines()
    if "quiet" in opts:
        return 0 if lines else 1
    prefix = os.environ.get("MOCKQ_PREFIX", "")
    result = []
    for k, line in enumerate(lines, 1):
        if "lower" in opts:
            line = line.lower()
        elif "upper" in opts:
            line = line.upper()
        if "number" in opts:
            line = f"{k}{sep}{line}"
        result.append(prefix + line + "\n")
    if out:
        try:
            with open(out, "w") as fh:
                fh.write("".join(result))
        except OSError as e:
            err(f"executable: {out}: {e.strerror}\n", 1)
    else:
        sys.stdout.write("".join(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
