import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

path = Path(__file__).resolve().parents[1]/"products/pacifica_muse/owner_start.py"
spec = importlib.util.spec_from_file_location("muse_owner_start", path)
owner = importlib.util.module_from_spec(spec); spec.loader.exec_module(owner)


class OwnerCredentialTests(unittest.TestCase):
    def test_existing_encrypted_credential_is_captured_not_printed(self):
        # Public dummy bytes; no real credential or decrypt process is used.
        result = subprocess.CompletedProcess([], 0, b"1"*88+b"\n", b"")
        with patch.object(owner.os, "geteuid", return_value=1000), patch.object(owner.subprocess, "run", return_value=result) as run:
            self.assertEqual(owner.agent_credential("/fixture/agent.key.cred"), "1"*88)
        command = run.call_args.args[0]
        self.assertEqual(command, ["sudo","-n","systemd-creds","decrypt","--name=agent.key","/fixture/agent.key.cred","-"])
        self.assertEqual(run.call_args.kwargs["stdout"], subprocess.PIPE)

    def test_decrypt_error_does_not_surface_output(self):
        result = subprocess.CompletedProcess([], 1, b"sensitive-dummy", b"sensitive-dummy")
        with patch.object(owner.subprocess,"run",return_value=result):
            with self.assertRaises(ValueError) as e: owner.agent_credential("/fixture/key")
        self.assertNotIn("sensitive-dummy", str(e.exception))

    def test_hidden_prompt_remains_available(self):
        with patch.object(owner.getpass,"getpass",return_value="1"*88):
            self.assertEqual(owner.agent_credential(), "1"*88)

    def test_bad_credential_encoding_rejected(self):
        for data in [b"x"*102, b"0"*88, b"bad"]:
            result = subprocess.CompletedProcess([], 0, data, b"")
            with patch.object(owner.subprocess,"run",return_value=result):
                with self.assertRaises(ValueError): owner.agent_credential("/fixture/key")


if __name__ == "__main__": unittest.main()
