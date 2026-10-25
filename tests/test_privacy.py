"""Execute the published examples together so their privacy contract is real."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


def examples():
    namespace = {"__name__": __name__}
    for filename in ["store-seam.md", "privacy.md"]:
        text = (ROOT / "references" / filename).read_text(encoding="utf-8")
        for source in re.findall(r"```python\n(.*?)\n```", text, flags=re.S):
            exec(compile(source, filename, "exec"), namespace)
    return namespace


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.ns = examples()
        self.backend = self.ns["FakeStore"]()
        self.a = self.ns["ScopedStore"](self.backend, "user-a")
        self.b = self.ns["ScopedStore"](self.backend, "user-b")

    def record(self, text="prefers dark mode"):
        return self.ns["Record"](id="", kind="fact", text=text)

    def test_same_fact_is_isolated_and_filter_precedes_limit(self):
        a_id = self.a.put(self.record())
        b_id = self.b.put(self.record())
        self.assertNotEqual(a_id, b_id)
        self.assertEqual(len(self.backend.records), 2)
        for store, record_id in [(self.a, a_id), (self.b, b_id)]:
            self.assertEqual([h.record.id for h in store.search("dark", k=1)], [record_id])

    def test_forged_scope_filter_cannot_read_other_tenant(self):
        self.b.put(self.record())
        self.assertEqual(self.a.search("dark", where={"scope": "user-b"}), [])

    def test_guessed_id_does_not_grant_read_or_delete(self):
        record_id = self.b.put(self.record())
        self.assertIsNone(self.a.get(record_id))
        self.assertFalse(self.a.delete(record_id))
        self.assertEqual(list(self.a.all()), [])
        self.assertIsNotNone(self.b.get(record_id))
        self.assertTrue(self.b.delete(record_id))

    def test_reused_input_and_return_values_cannot_change_stored_scope(self):
        record = self.record()
        a_id = self.a.put(record)
        self.b.put(record)
        self.assertEqual(record.metadata, {})
        hit = self.a.search("dark")[0]
        hit.record.metadata["scope"] = "user-b"
        self.assertEqual(self.a.get(a_id).metadata["scope"], "user-a")

    def test_backend_that_ignores_filter_fails_closed(self):
        self.b.put(self.record())
        self.backend.search = lambda *a, **kw: [self.ns["Hit"](next(self.backend.all()), 1, "bad")]
        with self.assertRaises(ValueError):
            self.a.search("dark")


if __name__ == "__main__":
    unittest.main()
