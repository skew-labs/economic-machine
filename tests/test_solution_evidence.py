"""A private checkpoint must not become an unreadable public manifest member."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import publish_solution_evidence as publisher

from economic_machine.values import MachineError, digest


class PublicMetadata(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / "artifacts/solution-launch"
        self.folder.mkdir(parents=True)
        self.source = self.root / "checkpoint.json"
        self.target = self.folder / "soak-complete.json"
        self.record = {
            "schema": "solution-qualification-1",
            "binding": {"pipeline_sha256": "a" * 64},
            "state": "COMPLETE",
        }
        self.write(self.record)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, record):
        self.source.write_text(json.dumps(record | {"checkpoint_sha256": digest(record)}))
        self.source.chmod(0o600)

    def test_publication_changes_copy_permissions_and_preserves_private_source(self):
        with patch.object(publisher, "ROOT", self.root):
            publisher.publish(self.source, self.target)
        self.assertEqual(self.source.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o644)
        self.assertEqual(json.loads(self.target.read_text()), json.loads(self.source.read_text()))

    def test_unknown_private_fields_never_publish_even_with_valid_hash(self):
        for record in [
            self.record | {"salt": "private-fixture"},
            self.record | {"binding": {"rpc_key": "private-fixture"}},
            self.record | {"jobs": "private-fixture"},
            self.record | {"state": "private-fixture"},
        ]:
            self.write(record)
            with patch.object(publisher, "ROOT", self.root), self.assertRaises(MachineError):
                publisher.publish(self.source, self.target)
            self.assertFalse(self.target.exists())

    def test_tampered_checkpoint_symlink_and_outside_path_fail_closed(self):
        self.source.write_text(self.source.read_text().replace("COMPLETE", "FORGED"))
        with patch.object(publisher, "ROOT", self.root), self.assertRaises(MachineError):
            publisher.publish(self.source, self.target)
        self.write(self.record)
        with patch.object(publisher, "ROOT", self.root), self.assertRaises(MachineError):
            publisher.publish(self.source, self.root / "soak-complete.json")
        self.target.symlink_to(self.source)
        with patch.object(publisher, "ROOT", self.root), self.assertRaises(MachineError):
            publisher.publish(self.source, self.target)
        self.assertEqual(self.source.stat().st_mode & 0o777, 0o600)
