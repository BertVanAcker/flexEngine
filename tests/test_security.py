import os
import json
import hmac
import hashlib
import secrets
import unittest
from unittest.mock import patch
from flexEngine.security.security import *
from flexEngine.utils.constants import *
class TestSecurityFunctions(unittest.TestCase):

    def setUp(self):
        # Backup current environment variables to restore later
        self.original_environ = os.environ.copy()

    def tearDown(self):
        # Restore environment variables after each test
        os.environ.clear()
        os.environ.update(self.original_environ)

    def test_generate_shared_secret_length(self):
        secret = generate_shared_secret(32)
        self.assertIsInstance(secret, bytes)
        self.assertEqual(len(secret), 32)
        # Ensure that successive calls produce different secrets
        secret2 = generate_shared_secret(32)
        self.assertNotEqual(secret, secret2)

    def test_store_shared_secret_bytes(self):
        # Given a bytes secret, store it and check that the value in the env is its hex representation.
        test_secret = b"abcdef"
        register_os_variable(test_secret, env_var_name=SECURITY_STORE.SHARED_SECRET)
        self.assertEqual(os.environ.get(SECURITY_STORE.SHARED_SECRET), test_secret.hex())

    def test_store_shared_secret_string(self):
        # Given a string secret, it should be stored as-is.
        test_secret = "mysecret"
        register_os_variable(test_secret, env_var_name=SECURITY_STORE.SHARED_SECRET)
        self.assertEqual(os.environ.get(SECURITY_STORE.SHARED_SECRET), test_secret)

    @patch("os.environ.get", return_value="testsecret")
    def test_compute_signature_expected(self, mock_get):
        # Prepare a sample command dictionary.
        command = {
            "id": "cmd1",
            "cmd": "TEST",
            "priority": 1,
            "expiry": 9999999999,
            "params": {"a": 1, "b": 2},
            "timestamp": 123456789,
            "nonce": "nonce"
        }
        # Manually compute what the signature should be.
        data_to_sign = (
            command["id"] +
            command["cmd"] +
            str(command["priority"]) +
            str(command["expiry"]) +
            json.dumps(command["params"], sort_keys=True) +
            str(command["timestamp"]) +
            command["nonce"]
        )
        expected_signature = hmac.new(b"testsecret", data_to_sign.encode(), hashlib.sha256).hexdigest()
        computed_signature = compute_signature(command)
        self.assertEqual(computed_signature, expected_signature)

    @patch("os.environ.get", return_value="testsecret")
    def test_verify_signature(self, mock_get):
        # Prepare a sample command and compute its signature.
        command = {
            "id": "cmd1",
            "cmd": "TEST",
            "priority": 1,
            "expiry": 9999999999,
            "params": {"a": 1, "b": 2},
            "timestamp": 123456789,
            "nonce": "nonce"
        }
        # Compute the signature and add it to the command.
        command["signature"] = compute_signature(command)
        # verify_signature should return True on an unmodified command.
        self.assertTrue(verify_signature(command))

        # Modify a field (e.g. command type) so the signature no longer matches.
        modified_command = command.copy()
        modified_command["cmd"] = "TEST_MODIFIED"
        self.assertFalse(verify_signature(modified_command))


if __name__ == '__main__':
    unittest.main()
