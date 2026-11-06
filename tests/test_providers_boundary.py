"""Structural enforcement of the sealed-box rule: these tests read the SOURCE (AST), so a violation fails the suite no matter what the runtime does.
  * a provider folder imports only the standard library, its own folder and app.providers.base (never another provider, never core);
  * the decision core imports only app.providers.base / registry / factory, never a concrete provider;
  * provider code never reads the process environment directly (it is handed its own config slot)."""
import ast
import os
import unittest
from tests import read_file

ROOT = os.path.join(os.path.dirname(__file__), "..", "app")
PROV = os.path.join(ROOT, "providers")
SHARED = {"base", "registry", "factory", "__init__"}
COMPOSITION_ROOTS = {"main.py", "seed.py", "paypal.py"}            # main/seed wire concrete providers; paypal.py is the one-line compatibility re-export


def providers():
    return sorted(d for d in os.listdir(PROV) if os.path.isdir(os.path.join(PROV, d)) and not d.startswith("_"))


def py_files(path):
    for dp, _, fs in os.walk(path):
        for f in fs:
            if f.endswith(".py"):
                yield os.path.join(dp, f)


def imports(path, pkg):
    """-> absolute dotted module names imported by `path` (relative imports resolved against `pkg`, the file's dotted package)."""
    tree = ast.parse(read_file(path))
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            if n.level:
                base = pkg.split(".")[: len(pkg.split(".")) - (n.level - 1)]
                mod = ".".join(base + ([n.module] if n.module else []))
            else:
                mod = n.module or ""
            out.append(mod)
            out += [mod + "." + a.name for a in n.names]
    return out


def pkg_of(path):
    rel = os.path.relpath(os.path.dirname(path), os.path.join(ROOT, ".."))
    return rel.replace(os.sep, ".")


class Boundary(unittest.TestCase):
    def test_there_is_at_least_paypal_and_mock(self):
        self.assertTrue({"paypal", "mock"} <= set(providers()))

    def test_a_provider_folder_imports_only_stdlib_its_own_folder_and_base(self):
        for name in providers():
            for f in py_files(os.path.join(PROV, name)):
                for m in imports(f, pkg_of(f)):
                    if not m.startswith("app"):
                        continue
                    ok = m == "app.providers" or m.startswith("app.providers." + name) or m == "app.providers.base" or m.startswith("app.providers.base.")
                    self.assertTrue(ok, "%s imports %s (a provider may import only its own folder and app.providers.base)" % (os.path.relpath(f, ROOT), m))

    def test_no_provider_names_another_provider_anywhere_in_its_source(self):
        names = providers()
        for name in names:
            for f in py_files(os.path.join(PROV, name)):
                src = read_file(f).lower()
                for other in names:
                    if other != name:
                        self.assertNotIn("providers." + other, src, "%s refers to the %s provider" % (os.path.relpath(f, ROOT), other))

    def test_the_decision_core_never_imports_a_concrete_provider(self):
        concrete = providers()
        for f in py_files(ROOT):
            rel = os.path.relpath(f, ROOT)
            if rel.startswith("providers" + os.sep) or os.path.basename(f) in COMPOSITION_ROOTS and os.path.dirname(f) == ROOT:
                continue
            for m in imports(f, pkg_of(f)):
                for c in concrete:
                    self.assertFalse(m == "app.providers." + c or m.startswith("app.providers." + c + "."), "%s imports the concrete provider %s" % (rel, c))
                self.assertFalse(m == "app.paypal", "%s imports the paypal compatibility shim" % rel)

    def test_provider_code_never_reads_the_process_environment(self):
        for name in providers():
            for f in py_files(os.path.join(PROV, name)):
                src = read_file(f)
                for bad in ("os.environ", "getenv(", "import os"):
                    self.assertNotIn(bad, src, "%s uses %s (providers are handed their own config slot)" % (os.path.relpath(f, ROOT), bad))

    def test_the_shared_modules_know_no_concrete_provider(self):
        for mod in ("base", "registry"):
            src = read_file(os.path.join(PROV, mod + ".py")).lower()
            for c in providers():
                self.assertNotIn("providers." + c, src)
                self.assertNotIn("import " + c, src)


if __name__ == "__main__":
    unittest.main()
