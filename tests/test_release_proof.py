import tempfile
import unittest
from pathlib import Path

from economic_machine.values import MachineError, digest
from machine_commerce.release_proof import file_evidence, safe_path, verify_manifest


class ReleaseProofTests(unittest.TestCase):
    def test_modified_proof_modified_file_missing_file_and_duplicate_reject(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); path = root / "input.txt"; path.write_text("real input")
            body = {"schema": "machine-release-proof-1", "created_at": 100,
                "files": [file_evidence(root, "input.txt")], "claims": {}, "assurance": "integrity"}
            manifest = body | {"manifest_sha256": digest(body)}
            self.assertTrue(verify_manifest(manifest, root)["accepted"])
            with self.assertRaisesRegex(MachineError, "MANIFEST_HASH"):
                verify_manifest(manifest | {"created_at": 101}, root)
            duplicate = body | {"files": body["files"] * 2}
            with self.assertRaises(MachineError):
                verify_manifest(duplicate | {"manifest_sha256": digest(duplicate)}, root)
            path.write_text("fake input")
            with self.assertRaisesRegex(MachineError, "FILE_HASH"): verify_manifest(manifest, root)
            path.unlink()
            with self.assertRaisesRegex(MachineError, "FILE_REQUIRED"): verify_manifest(manifest, root)

    def test_absolute_parent_escape_and_symlink_reject(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / "source").write_text("input")
            (root / "link").symlink_to(root / "source")
            for name in ["/etc/passwd", "../source", "link"]:
                with self.subTest(name=name), self.assertRaises(MachineError): safe_path(root, name)
