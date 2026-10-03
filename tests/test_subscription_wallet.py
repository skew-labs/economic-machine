import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from provision_subscription_wallet import provision, validate_vault


class SubscriptionWalletTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = tempfile.TemporaryDirectory()
        cls.vault = Path(cls.template.name) / "treasury" / "receiver"
        cls.public, _ = provision(cls.vault)

    @classmethod
    def tearDownClass(cls):
        cls.template.cleanup()

    def setUp(self):
        import shutil
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.copy = Path(self.temp.name) / "treasury" / "receiver"
        self.copy.parent.mkdir(mode=0o700)
        shutil.copytree(self.vault, self.copy)

    def test_encrypted_roundtrip_and_idempotent_recipient_without_output(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            public, created = provision(self.copy)
        self.assertFalse(created)
        self.assertEqual(public, self.public)
        self.assertEqual(validate_vault(self.copy), public)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(set(public), {"address", "network", "purpose", "created_at"})
        keystore = json.loads((self.copy / "keystore.json").read_bytes())
        self.assertEqual(keystore["version"], 3)
        self.assertNotIn("private_key", keystore)
        self.assertEqual(self.copy.stat().st_mode & 0o777, 0o700)
        for item in self.copy.iterdir():
            self.assertEqual(item.stat().st_mode & 0o777, 0o600)

    def test_partial_vault_is_never_replaced(self):
        (self.copy / "passphrase").unlink()
        with self.assertRaises(ValueError):
            provision(self.copy)
        self.assertFalse((self.copy / "passphrase").exists())

    def test_insecure_parent_directory_or_file_is_rejected(self):
        self.copy.parent.chmod(0o755)
        with self.assertRaises(ValueError):
            provision(self.copy)
        self.copy.parent.chmod(0o700)
        (self.copy / "passphrase").chmod(0o644)
        with self.assertRaises(ValueError):
            provision(self.copy)

    def test_symlinks_and_hardlinks_are_rejected(self):
        original = self.copy / "passphrase"
        target = self.copy.parent / "secret"
        original.rename(target)
        original.symlink_to(target)
        with self.assertRaises(ValueError):
            provision(self.copy)
        original.unlink()
        os.link(target, original)
        with self.assertRaises(ValueError):
            provision(self.copy)
        alias = self.copy.parent / "alias"
        alias.symlink_to(self.copy, target_is_directory=True)
        with self.assertRaises(ValueError):
            provision(alias)

    def test_mismatched_public_address_is_rejected(self):
        path = self.copy / "public.json"
        path.write_text(json.dumps(self.public | {"address": "0x" + "11" * 20}))
        with self.assertRaises(ValueError):
            provision(self.copy)

    def test_weak_kdf_is_rejected_before_decryption(self):
        path = self.copy / "keystore.json"
        data = json.loads(path.read_bytes())
        data["crypto"]["kdfparams"]["n"] = 4096
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            provision(self.copy)
