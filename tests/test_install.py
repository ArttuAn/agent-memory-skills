import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("install_safe", ROOT / "tools/install_safe.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "source"
        for directory in ["skills/example", "commands", "references", "schema", "tools"]:
            (self.root / directory).mkdir(parents=True)
        (self.root / "skills/example/SKILL.md").write_text("example")
        (self.root / "commands/example.md").write_text("command")
        self.dest = self.base / "target"
        self.commands = self.base / "commands"

    def install(self):
        installer.install(self.root, self.dest, self.commands)

    def link(self, target, destination, directory=False):
        try:
            destination.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            self.skipTest(f"Symlink creation unavailable: {exc}")

    def test_repeat_install_preserves_unrelated_files(self):
        self.install()
        target = self.dest / (installer.PREFIX + "example")
        (target / "user-note.txt").write_text("keep me")
        self.install()
        self.assertEqual((target / "SKILL.md").read_text(), "example")
        self.assertEqual((target / "user-note.txt").read_text(), "keep me")

    def test_command_symlink_never_overwrites_outside_file(self):
        victim = self.base / "private.txt"
        victim.write_text("private")
        self.commands.mkdir()
        self.link(victim, self.commands / "example.md")
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(victim.read_text(), "private")
        self.assertFalse(self.dest.exists(), "preflight must run before any copies")

    def test_linked_destination_ancestor_is_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        self.link(outside, self.dest, directory=True)
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(list(outside.iterdir()), [])

    def test_source_link_is_rejected(self):
        victim = self.base / "secret"
        victim.write_text("private")
        self.link(victim, self.root / "references/secret.md")
        with self.assertRaises(ValueError):
            self.install()
        self.assertFalse(self.dest.exists())

    def test_source_overlap_is_rejected(self):
        self.dest = self.root / "skills"
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual((self.root / "skills/example/SKILL.md").read_text(), "example")

    def test_existing_hard_link_is_replaced_without_truncating_original(self):
        import os
        victim = self.base / "private.txt"
        victim.write_text("private")
        self.commands.mkdir()
        os.link(victim, self.commands / "example.md")
        self.install()
        self.assertEqual(victim.read_text(), "private")
        self.assertEqual((self.commands / "example.md").read_text(), "command")


if __name__ == "__main__":
    unittest.main()
