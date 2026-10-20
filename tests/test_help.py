"""`dt.py help` and the README list every command the client runs.

By 20 Oct the help named 19 of 33 commands while every usage error said "dt.py help lists every
command" — so a run that trusted the hint was told the client could not do things it could. The list of
commands is read from the dispatch itself, so a new command fails here until it is documented.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def commands():
    src = read("dt.py")
    found = set(re.findall(r'^\s+(?:el)?if cmd == "([a-z-]+)":', src, re.M))
    for group in re.findall(r'^\s+(?:el)?if cmd in \(([^)]*)\):', src, re.M):
        found.update(re.findall(r'"([a-z-]+)"', group))
    return {c for c in found if not c.startswith("-")} - {"help"}


class Help(unittest.TestCase):

    def test_the_dispatch_is_found(self):
        self.assertGreater(len(commands()), 30)

    def test_the_help_names_every_command(self):
        doc = read("dt.py").split('"""')[1]
        missing = sorted(c for c in commands() if not re.search(r"dt\.py %s\b" % re.escape(c), doc))
        self.assertEqual(missing, [], "commands `dt.py help` does not list")

    def test_the_readme_names_every_command(self):
        readme = read("README.md")
        missing = sorted(c for c in commands() if not re.search(r"dt\.py %s\b" % re.escape(c), readme))
        self.assertEqual(missing, [], "commands the README does not list")


if __name__ == "__main__":
    unittest.main()
