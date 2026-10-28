"""Check the shipped decision-client bytecode, not only the source tree."""
import json
import marshal
from pathlib import Path
import sys
import types
import unittest
import zlib


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@unittest.skipUnless(sys.version_info[:2] == (3, 11), "The EXE uses Python 3.11 bytecode")
class ShippedPayloadTests(unittest.TestCase):
    def test_decision_client_payload_matches_source_and_rejects_nan(self):
        manifest = json.loads((ROOT / "patch" / "manifest.json").read_text(encoding="utf-8"))
        toc = dict(marshal.loads((ROOT / "patch" / manifest["pyz"]["toc_file"]).read_bytes()))
        _, offset, length = toc["core.jev_client"]
        start = offset - manifest["pyz"]["toc_offset"]
        appended = (ROOT / "patch" / manifest["pyz"]["append_file"]).read_bytes()
        self.assertGreaterEqual(start, 0)
        self.assertLessEqual(start + length, len(appended))
        code = marshal.loads(zlib.decompress(appended[start:start + length]))
        expected = compile((ROOT / "src" / "core" / "jev_client.py").read_text(encoding="utf-8"),
                           code.co_filename, "exec", dont_inherit=True, optimize=0)
        self.assertEqual(code, expected, "Rebuild patch payload after modifying src/core/jev_client.py")

        module = types.ModuleType("core._shipped_jev_client")
        module.__package__ = "core"
        exec(code, module.__dict__)
        body = {"choices": [{"message": {"content":
            '{"answers":{"literal":{"noul":"NaN"}}}'}}]}
        with self.assertRaises(module.JevError):
            module._answers_of({"literal": {"type": "noul"}}, body)


if __name__ == "__main__":
    unittest.main()
