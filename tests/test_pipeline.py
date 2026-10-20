"""Regression tests: unit tests of the parsers and an end-to-end run of steps 1-4 on the mock CLI.

    python3 -m unittest -v tests/test_pipeline.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

FW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(FW, "lib"))

import argvparse  # noqa: E402
import errtemplate  # noqa: E402
import features  # noqa: E402

IFACE = {"options": [
    {"name": "--upper", "aliases": ["-u"], "takes_value": False},
    {"name": "--number", "aliases": ["-n"], "takes_value": False},
    {"name": "--sep", "aliases": ["-s"], "takes_value": "required"},
    {"name": "--color", "aliases": [], "takes_value": "optional"}]}


class ArgvParse(unittest.TestCase):
    def parse(self, argv, path=()):
        return argvparse.parse(argv, list(path), IFACE)

    def test_aliases_values_and_bundles(self):
        p = self.parse(["-un", "-s", ":", "--color", "f.txt"])
        self.assertEqual(p["options"], {"--upper": 1, "--number": 1, "--sep": 1, "--color": 1})
        self.assertEqual(p["values"], {"--sep": [":"]})
        self.assertEqual(p["positionals"], ["f.txt"])
        p = self.parse(["--sep=;", "-s,", "--color=always"])
        self.assertEqual(p["values"], {"--sep": [";", ","], "--color": ["always"]})

    def test_separator_unknown_negative_number(self):
        p = self.parse(["--", "-u", "-3"])
        self.assertEqual(p["options"], {})
        self.assertEqual(p["positionals"], ["-u", "-3"])
        p = self.parse(["-Z", "-3", "--nope"])
        self.assertEqual(p["unknown"], ["-Z", "--nope"])
        self.assertEqual(p["positionals"], ["-3"])

    def test_missing_value_and_command_path(self):
        p = self.parse(["-s"])
        self.assertEqual(p["missing_value"], ["--sep"])
        self.assertFalse(self.parse(["x", "-u"], ["count"])["parse_ok"])
        self.assertEqual(self.parse(["count", "-u"], ["count"])["options"], {"--upper": 1})

    def test_remove_option(self):
        argv = ["-n", "-s", "|", "a.txt"]
        self.assertEqual(argvparse.remove_option(argv, self.parse(argv), "--sep"), ["-n", "a.txt"])
        argv = ["-un", "a.txt"]
        self.assertIsNone(argvparse.remove_option(argv, self.parse(argv), "--upper"))


class ErrTemplate(unittest.TestCase):
    def test_slots_whole_words_only(self):
        parts = errtemplate.template(b"error: unexpected argument '--hel' found; did you mean --help?\n",
                                     ["--hel"])
        self.assertEqual(errtemplate.render(parts), "error: unexpected argument '{argv[0]}' found; did you mean --help?\n")
        parts = errtemplate.template(b"error: invalid value 'ength' for '--length <LEN>'\n", ["-length", "4"])
        self.assertEqual(errtemplate.render(parts), "error: invalid value 'ength' for '--length <LEN>'\n")
        parts = errtemplate.template(b"error: unexpected argument '--hel' found; did you mean --help?\n", ["--hel"])
        self.assertEqual(errtemplate.expand(parts, ["--hel"]),
                         "error: unexpected argument '--hel' found; did you mean --help?\n")


class Candidates(unittest.TestCase):
    def test_read(self):
        import sweep
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p1, p2 = os.path.join(d, "a.txt"), os.path.join(d, "b.txt")
            with open(p1, "w") as f:
                f.write("a to z;\n# comment;\n\n  \na to z;\n")
            with open(p2, "w") as f:
                f.write("let x = 1;\nx;\n---\n\n---\nlet .x = { 1; }\n.x;\n---\n")
            self.assertEqual(sweep.read_candidates([p1], ["z"]), ["a to z;", "# comment;", "z"])
            self.assertEqual(sweep.read_candidates([p2], None), ["let x = 1;\nx;\n", "let .x = { 1; }\n.x;\n"])

    def test_auto_lists(self):
        import sweep
        self.assertEqual(sweep.auto_lists("vocab:symbol"), ["symbol_names"])
        self.assertEqual(sweep.auto_lists("vocab:unicode-category"), ["unicode_categories"])
        self.assertEqual(sweep.auto_lists("vocab:function"), ["math_functions"])
        self.assertEqual(sweep.auto_lists("vocab:shell"), [])

    def test_split(self):
        import sweep
        blobs = type("B", (), {"put": lambda self, d: "sha"})()
        t = sweep.instantiate({"argv": ["{C}", "f.txt"], "env": {}}, "-n --sep ' x'", blobs, {}, "{C}", True)
        self.assertEqual(t["argv"], ["-n", "--sep", " x", "f.txt"])
        t = sweep.instantiate({"argv": ["-e", "x{C}"], "env": {}}, "a b", blobs, {}, "{C}", True)
        self.assertEqual(t["argv"], ["-e", "xa b"])
        t = sweep.instantiate({"argv": ["-e", "{C}"], "env": {}}, "a b", blobs, {}, "{C}", False)
        self.assertEqual(t["argv"], ["-e", "a b"])


class LeakExtraction(unittest.TestCase):
    def ids(self, text):
        return [i["id"] for i in features.extract(text, "t")]

    def test_parser_rule_names(self):
        self.assertEqual(self.ids("  = expected literal, raw, not, range, quantifier_quantity, or variable_invocation"),
                         ["rule:literal", "rule:raw", "rule:not", "rule:range", "rule:quantifier_quantity",
                          "rule:variable_invocation"])
        self.assertEqual(self.ids("  = expected amount, class_content, or assertion_type"),
                         ["rule:amount", "rule:class_content", "rule:assertion_type"])
        self.assertEqual(self.ids("Syntax Error: Function 'sin' expected parentheses"), [])
        self.assertEqual(self.ids("expected EOI"), [])

    def test_values_words_vocab(self):
        self.assertEqual(self.ids("  -c, --color <WHEN>  Colors [possible values: auto, always, never]"),
                         ["value:--color=auto", "value:--color=always", "value:--color=never"])
        self.assertEqual(self.ids("error: unknown function 'foo'"), ["vocab:function"])
        self.assertEqual(self.ids("Error: unrecognized symbol <tabb>"), ["vocab:symbol"])
        self.assertEqual(self.ids("Error: usage of an unrecognized unicode category"), ["vocab:unicode_category"])
        self.assertEqual(self.ids("Error: usage of an unrecognized symbol"), ["vocab:symbol"])
        self.assertEqual(self.ids("error: invalid syntax"), [])
        self.assertEqual(self.ids("error: unexpected argument '--x' found\n  tip: did you mean 'run'?"), ["word:run"])
        self.assertEqual(self.ids("error: invalid value 'x' for '--n <N>'"), [])
        self.assertEqual(self.ids("error: unknown option '--frob'"), [])


def pk(ws, *args, ok=True):
    env = dict(os.environ, PK_WORK=os.path.join(ws, "work"))
    r = subprocess.run([sys.executable, os.path.join(FW, "pk"), *args], cwd=ws, env=env,
                       capture_output=True, text=True)
    if ok and r.returncode:
        raise AssertionError(f"pk {' '.join(args)} failed:\n{r.stdout}\n{r.stderr}")
    return r


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ws = tempfile.mkdtemp(prefix="pk4test-")
        docs = os.path.join(cls.ws, "gold")
        os.makedirs(docs)
        shutil.copy(os.path.join(FW, "tests", "mock", "mockq.py"), os.path.join(docs, "executable"))
        os.chmod(os.path.join(docs, "executable"), 0o755)
        with open(os.path.join(docs, "README.md"), "w") as f:
            f.write("mockq docs. MOCKQ_PREFIX prefixes lines.\n")
        cls.work = os.path.join(cls.ws, "work")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.ws, ignore_errors=True)

    def w(self, *p):
        return os.path.join(self.work, *p)

    def jload(self, *p):
        with open(self.w(*p)) as f:
            return json.load(f)

    def jdump(self, obj, *p):
        os.makedirs(os.path.dirname(self.w(*p)), exist_ok=True)
        with open(self.w(*p), "w") as f:
            json.dump(obj, f)

    def t(self, sub, *args):
        out = pk(self.ws, "t", sub, *args).stdout
        return out.split()[0]

    def test_pipeline(self):
        ws = self.ws
        # ---- step 1
        out = pk(ws, "discover", os.path.join(ws, "gold", "executable")).stdout
        self.assertIn("count", out)
        self.jdump(self.jload("docs_index.draft.json"), "docs_index.json")
        pk(ws, "check", "1")
        # ---- step 2 (root)
        for s in ("root", "count"):
            pk(ws, "iface", "draft", s)
            shutil.copy(self.w(s, "interface.draft.json"), self.w(s, "interface.json"))
        iface = self.jload("root", "interface.json")
        self.assertEqual({o["name"] for o in iface["options"]},
                         {"--upper", "--lower", "--number", "--quiet", "--sep", "--output", "--eval", "--help",
                          "--version"})
        for o in iface["options"]:
            if o["name"] == "--output":
                o["context"] = True
        self.jdump(iface, "root", "interface.json")
        self.assertEqual([e["name"] for e in iface["env_vars"]], ["MOCKQ_PREFIX"])
        os.makedirs(self.w("fixtures", "root"))
        with open(self.w("fixtures", "root", "a.txt"), "w") as f:
            f.write("Hello World\nfoo Bar\n")
        r = pk(ws, "t", "root", "--file", "a.txt", "a.txt", ok=False)
        self.assertIn("lone --", r.stdout + r.stderr)
        base = self.t("root", "--file", "a.txt", "--note", "plain", "--", "a.txt")
        up = self.t("root", "--file", "a.txt", "--", "-u", "a.txt")
        sep = self.t("root", "--file", "a.txt", "--", "-s", " | ", "a.txt")
        nsep = self.t("root", "--file", "a.txt", "--", "-n", "-s", " | ", "a.txt")
        self.t("root", "--file", "a.txt", "--env", "MOCKQ_PREFIX=> ", "--", "a.txt")
        o_plain = self.t("root", "--file", "a.txt", "--", "-o", "out.txt", "a.txt")
        o_upper = self.t("root", "--file", "a.txt", "--", "-o", "out.txt", "-u", "a.txt")
        out = pk(ws, "t", "root", "--tag", "lang", "--covers", "prior:call", "--", "-e", "double(2)").stdout
        call = out.split()[0]
        self.assertIn("not in the checklist yet", out)
        out = pk(ws, "t", "root", "--", "--crash").stdout
        crash = out.split()[0]
        self.assertIn("digits masked", out)
        self.assertNotIn("NONDETERMINISTIC", out)
        out = pk(ws, "t", "root", "--tui", "j j q", "--tui-delay", "0.3", "--", "--pager").stdout
        self.assertIn("--- screen 2 ---\npage 3", out)
        self.assertIn("exit=0", out)
        out = pk(ws, "t", "root", "--tui", "j", "--tui-delay", "0.3", "--", "--pager").stdout
        self.assertIn("still running", out)
        self.assertNotIn("NONDETERMINISTIC", out)
        q = self.t("root", "--file", "a.txt", "--", "-q", "a.txt")
        n = self.t("root", "--file", "a.txt", "--", "-n", "a.txt")
        qn = self.t("root", "--file", "a.txt", "--", "-q", "-n", "a.txt")
        nq = self.t("root", "--file", "a.txt", "--", "-n", "-q", "a.txt")
        lo = self.t("root", "--file", "a.txt", "--", "-l", "a.txt")
        ul = self.t("root", "--file", "a.txt", "--", "-u", "-l", "a.txt")
        lu = self.t("root", "--file", "a.txt", "--", "-l", "-u", "a.txt")
        self.jdump([{"a": "--quiet", "b": "--number", "class": "II",
                     "tests": {"base": base, "a": q, "b": n, "ab": qn, "ba": nq}, "reason": "r"},
                    {"a": "--upper", "b": "--lower", "class": "IV", "dominant": "--lower",
                     "tests": {"base": base, "a": up, "b": lo, "ab": ul, "ba": lu}, "reason": "r"},
                    {"a": "--number", "b": "--upper", "class": "I",
                     "tests": {"base": base, "a": n, "b": up, "ab": ul}, "reason": "wrong on purpose"}],
                   "root", "relations.json")
        cov = pk(ws, "coverage", "root").stdout
        self.assertIn("all identical to their controls (inert); if it only acts together", cov)
        self.assertIn("PROBLEMS: I --number on --upper", cov)
        rel = self.jload("root", "relations.json")
        rel[2] = {"a": "--sep", "b": "--number", "class": "I",
                  "tests": {"base": base, "a": sep, "b": n, "ab": nsep}, "reason": "separator needs numbering"}
        self.jdump(rel, "root", "relations.json")
        cov = pk(ws, "coverage", "root").stdout
        self.assertIn(f"rule tests: base {base}", cov)
        self.assertIn("--sep                        triggered only with --number", cov)
        self.assertIn("gaps: none", cov)
        self.assertIn("--output                     triggered (context option", cov)
        self.assertIn("unexpected argument", cov)
        # ---- checklist: leaks, gate, failed attempts, sweep
        pk(ws, "t", "root", "--tag", "lang", "--", "-e", "()")
        self.assertIn("rule:function_call", pk(ws, "features", "root").stdout)
        out = pk(ws, "t", "root", "--tag", "lang", "--", "-e", "foo(1)").stdout
        self.assertIn("NEW CHECKLIST ITEMS", out)
        self.assertIn("vocab:function", out)
        r = pk(ws, "finish", "root", ok=False)
        self.assertIn("ready", r.stdout + r.stderr)
        r = pk(ws, "ready", "root", ok=False)
        self.assertIn("FINDINGS.md", r.stdout)
        self.assertIn("features.json missing", r.stdout)
        with open(self.w("root", "FINDINGS.md"), "w") as f:
            f.write("## Behaviour summary\n- ok\n")
        feats = [{"id": "prior:call", "kind": "construct", "source": "prior", "description": "function call"},
                 {"id": "rule:function_call", "tests": [call]},
                 {"id": "err:unclosed", "kind": "error", "source": "error", "description": "unclosed parenthesis"},
                 {"id": "prior:hex", "kind": "construct", "source": "prior", "description": "hex literal 0x10"}]
        self.jdump(feats, "root", "features.json")
        r = pk(ws, "ready", "root", ok=False)
        for item in ("rule:number", "rule:group", "vocab:function", "err:unclosed", "prior:hex"):
            self.assertIn(f"checklist item {item} ", r.stdout)
        self.assertNotIn("checklist item prior:call ", r.stdout)
        self.assertNotIn("checklist item rule:function_call ", r.stdout)
        self.t("root", "--covers", "rule:number", "--", "-e", "5")
        out = pk(ws, "t", "root", "--covers", "rule:group", "--", "-e", "(").stdout
        self.assertIn("exit=1", out)
        self.t("root", "--covers", "rule:group", "--", "-e", "(3)")
        self.t("root", "--covers", "err:unclosed", "--", "-e", "double(1")
        self.t("root", "--covers", "prior:hex", "--", "-e", "0x10")
        feats[3]["unreachable"] = "the program has no hex literals"
        self.jdump(feats, "root", "features.json")
        r = pk(ws, "ready", "root", ok=False)
        self.assertIn("checklist item prior:hex", r.stdout)
        self.assertIn("(have 1)", r.stdout)
        self.t("root", "--covers", "prior:hex", "--", "-e", "0X10")
        r = pk(ws, "ready", "root", ok=False)
        self.assertIn("(have 2)", r.stdout)  # 2 failed attempts are no longer enough
        with open(self.w("fixtures", "root", "hex.txt"), "w") as f:
            f.write("0x10\n0X10\nx10\n#10\n$10\n16h\n0h10\nhex(10)\n0x1F\n")
        r = pk(ws, "batch", "root", "--cands", "hex.txt", "--covers", "prior:hex", "--", "-e", "x", ok=False)
        self.assertIn("no {C}", r.stdout + r.stderr)
        out = pk(ws, "batch", "root", "--cands", "hex.txt", "--covers", "prior:hex", "--", "-e", "{C}").stdout
        self.assertIn("batch B01: 9 candidates, 0 accepted", out)
        self.assertIn("none accepted", out)
        self.assertIn("root-b0001", out)
        out = pk(ws, "batch", "root", "--cand", "-e double(2)", "--cand", "--eval sq(3)", "--cand", "-x 1",
                 "--split", "--covers", "prior:call", "--", "{C}").stdout
        self.assertIn("batch B02: 3 candidates, 2 accepted", out)
        self.assertIn("-e double(2)", out)
        with open(self.w("fixtures", "root", "words.txt"), "w") as f:
            f.write("double\nneg\nsq\nbogus\n")
        r = pk(ws, "sweep", "root", "--list-file", "words.txt", "--covers", "vocab:function", "--", "-e", "x(3)",
               ok=False)
        self.assertIn("no {W}", r.stdout + r.stderr)
        out = pk(ws, "sweep", "root", "--list", "math_functions", "--list-file", "words.txt",
                 "--covers", "vocab:function", "--", "-e", "{W}(3)").stdout
        self.assertIn(" 3 accepted", out)
        self.assertIn("    sq                       -> 9", out)
        self.assertIn("REJECTED (majority): error: unknown function '{W}'", out)
        out = pk(ws, "features", "root").stdout
        self.assertIn("[covered] vocab:function/sq", out)
        self.assertIn("[unreachable] prior:hex", out)
        self.assertIn("open 0", out)
        listing = pk(ws, "tests", "root").stdout
        self.assertIn("sweep word tests not listed", listing)
        pk(ws, "check", "2r", "root", ok=False)
        pk(ws, "ready", "root")
        pk(ws, "check", "2r", "root")
        pk(ws, "finish", "root")
        cl = self.jload("root", "clusters.json")
        ids = {c["id"] for c in cl["clusters"]}
        self.assertTrue({"default", "errors", "option:--upper", "option:--sep", "env:MOCKQ_PREFIX"} <= ids)
        self.assertNotIn(qn, [t for c in cl["clusters"] if c["id"] != "errors" for t in c["kept"]])
        self.assertIn(qn, cl["kept"])
        upper = next(c for c in cl["clusters"] if c["id"] == "option:--upper")
        self.assertIn({"test": o_upper, "control": o_plain}, upper["witness"])
        ctx = next(c for c in cl["clusters"] if c["id"] == "option:--output")
        self.assertTrue(ctx["context"] and ctx["kept"])
        r = pk(ws, "t", "root", "--", "-u", ok=False)
        self.assertIn("finished", r.stdout + r.stderr)
        sw = next(c for c in cl["clusters"] if c["id"] == "sweep:S01")
        self.assertGreater(len(sw["members"]), 80)
        self.assertEqual(len(sw["kept"]), 5)  # 3 accepted words + 2 representatives of the rejection
        fs = {f["id"]: f for f in cl["features"]}
        self.assertEqual(fs["prior:hex"]["status"], "unreachable")
        b1 = next(c for c in cl["clusters"] if c["id"] == "sweep:B01")
        self.assertEqual(len(b1["members"]), 9)
        self.assertLessEqual(len(b1["kept"]), 6)  # one representative per rejection, at most 6
        b2 = next(c for c in cl["clusters"] if c["id"] == "sweep:B02")
        self.assertEqual(len(b2["kept"]), 3)  # 2 accepted + the single rejection
        self.assertIn("root-b0010", fs["prior:call"]["tests"])
        self.assertIn(call, cl["kept"])
        # ---- step 2 (count)
        os.makedirs(self.w("fixtures", "count"))
        with open(self.w("fixtures", "count", "w.txt"), "w") as f:
            f.write("a b c\nd e\n")
        self.t("count", "--file", "w.txt", "--", "count", "w.txt")
        self.t("count", "--file", "w.txt", "--", "count", "-w", "w.txt")
        self.jdump([], "count", "relations.json")
        pk(ws, "coverage", "count")
        with open(self.w("count", "FINDINGS.md"), "w") as f:
            f.write("## Behaviour summary\n")
        self.jdump([], "count", "features.json")
        pk(ws, "ready", "count")
        pk(ws, "finish", "count")
        # ---- step 3
        for s in ("root", "count"):
            pk(ws, "spec", "prepare", s)
            cl = self.jload(s, "clusters.json")
            for u in self.jload(s, "spec_input", "units.json"):
                unit, path = u["unit"], u["write"]
                if unit == "errors":
                    tpl = [t["id"] for c in cl["clusters"] if c["id"] == "errors" for t in c["templates"]]
                    doc = {"entries": [{"id": "E01", "title": "all", "causes": [{"text": "x", "members": tpl}]}]}
                elif unit in ("default", "overview"):
                    doc = {"text": unit}
                elif unit == "features":
                    ids = [f["id"] for f in cl["features"] if f["status"] != "open" and "/" not in f["id"]]
                    doc = {"entries": [{"ids": ids[:-1], "title": "most", "text": "x"}]}
                    self.jdump(doc, s, path)
                    r = pk(ws, "spec", "check", s, "--unit", "features", ok=False)
                    self.assertIn(f"checklist item {ids[-1]} not described", r.stdout)
                    doc = {"entries": [{"ids": ids, "title": "all features", "text": "x"}]}
                elif unit.startswith("option:"):
                    c = next(c for c in cl["clusters"] if c["id"] == unit)
                    wt = c["kept"] if c.get("context") else [p["test"] for p in c.get("witness", []) + c.get("dependent", [])]
                    doc = {"option": unit[7:], "blocks": [{"given": "g", "when": "w", "then": "t",
                                                            "assert": [{"id": "A1", "text": "x"}],
                                                            "basis": wt[:1] or ["help"]}]}
                elif unit.startswith("env:"):
                    doc = {"var": unit[4:], "values": "", "default": "", "precedence": "", "text": "t"}
                else:
                    cls = ("II",) if unit == "exclusions" else ("IV", "I")
                    doc = {"entries": [{"a": r["a"], "b": r["b"], "dominant": r.get("dominant", r["a"]),
                                        "description": "d"} for r in cl["relations"] if r["class"] in cls]}
                self.jdump(doc, s, path)
        with open(self.w("fixtures", "root", "b.txt"), "w") as f:
            f.write("One\ntwo\n\n")
        r = pk(ws, "verify", "root", "--option", "--upper", "--assert", "A1", "--why", "same", "--file", "a.txt",
               "--", "-u", "a.txt", ok=False)
        self.assertIn("identical to basis", r.stdout + r.stderr)
        out = pk(ws, "verify", "root", "--option", "--upper", "--assert", "A1", "--why", "empty line",
                 "--file", "b.txt", "--", "-u", "b.txt").stdout
        vid = out.split()[0]
        self.assertIn("effect of --upper visible: yes", out)
        r = pk(ws, "verify", "root", "--option", "--upper", "--assert", "A1", "--why", "x", "--", "-u", ok=False)
        self.assertIn("already verified", r.stdout + r.stderr)
        r = pk(ws, "spec", "build", "root", ok=False)
        self.assertIn("has no verdict", r.stdout)
        doc = self.jload("root", "sections", "opt--upper.json")
        doc["verify"] = [{"assert": "A1", "test": vid, "new_aspect": "x", "verdict": "inconsistent", "reason": "r"}]
        self.jdump(doc, "root", "sections", "opt--upper.json")
        r = pk(ws, "spec", "build", "root", ok=False)
        self.assertIn("must be revised", r.stdout)
        doc["verify"][0]["revised"] = True
        doc["blocks"][0]["basis"].append(vid)
        self.jdump(doc, "root", "sections", "opt--upper.json")
        pk(ws, "spec", "build", "root")
        pk(ws, "spec", "build", "count")
        with open(self.w("root", "SPEC.md")) as f:
            spec = f.read()
        self.assertIn("### Cannot be combined", spec)
        self.assertIn("`--lower` takes effect", spec)
        self.assertIn("`--sep` has an effect only together with `--number`", spec)
        self.assertIn("### Input features", spec)
        index = self.jload("root", "entry_index.json")
        self.assertNotIn(["unmapped"], list(index.values()))
        self.assertIn(crash, index)
        with open(self.w("root", "spec_input", "01-errors.md")) as f:
            self.assertIn("thread 'main' (<N>) panicked", f.read())
        self.assertEqual(index[vid], ["option:--upper"])
        # ---- step 4
        os.makedirs(self.w("impl"))
        shutil.copy(os.path.join(FW, "tests", "mock", "mockq.py"), self.w("impl", "main.py"))
        with open(self.w("impl", "compile.sh"), "w") as f:
            f.write("#!/bin/sh\nset -e\ncp main.py executable\nchmod +x executable\n")
        out = pk(ws, "diff").stdout
        total = int(out.split("pass ")[1].split("/")[1].split()[0])
        passed = int(out.split("pass ")[1].split("/")[0])
        self.assertGreaterEqual(passed, total - 3)  # only python tracebacks naming the file path differ
        rows = [json.loads(l) for l in open(sorted(os.path.join(self.w("diff"), d, "results.jsonl")
                                                   for d in os.listdir(self.w("diff")) if d.startswith("run"))[-1])]
        self.assertTrue(next(r for r in rows if r["id"] == crash)["pass"])
        with open(self.w("impl", "main.py")) as f:
            src = f.read().replace("cannot be used with", "conflicts with")
        with open(self.w("impl", "main.py"), "w") as f:
            f.write(src)
        out = pk(ws, "diff").stdout
        self.assertIn("regressions", out)
        self.assertIn("root:exclusions:--quiet+--number", out)
        detail = pk(ws, "diff", "--test", qn).stdout
        self.assertIn("conflicts with", detail)
        dest = os.path.join(ws, "dest")
        os.makedirs(dest)
        pk(ws, "export", dest)
        subprocess.run(["sh", os.path.join(dest, "compile.sh")], check=True, capture_output=True)
        r = subprocess.run([os.path.join(dest, "executable"), "-u"], input="q\n", capture_output=True, text=True)
        self.assertEqual(r.stdout, "Q\n")


if __name__ == "__main__":
    unittest.main()
