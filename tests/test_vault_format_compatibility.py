"""Read real pre-refactor *synthetic* ciphertext, rather than generating it with new code."""

import json
import os
from pathlib import Path
import tempfile
import unittest

from xvviix.storage.vault import VaultStore
from tests.support import PROJECT_ROOT, NEW_PASSWORD

VECTORS = json.loads(
    (PROJECT_ROOT / "tests/fixtures/pre_refactor_vaults.json").read_text(encoding="utf-8")
)


class VaultFormatCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="xvviix-old-format-")
        self.addCleanup(self.directory.cleanup)
        self.store = VaultStore(self.directory.name)

    def install_vector(self, version, local_recovery=False):
        vector = VECTORS["vaults"][version]
        payload = json.dumps(vector["metadata"]).encode("utf-8")
        self.store._atomic_write_bytes(self.store.VAULT_FILE, payload)
        self.store._atomic_write_bytes(self.store.VAULT_FILE + ".bak", payload)
        for name, ciphertext in vector["files"].items():
            self.store._atomic_write_bytes(
                str(Path(self.directory.name) / name), ciphertext.encode("utf-8")
            )
        if local_recovery:
            self.store._atomic_write_bytes(
                self.store.LOCAL_RECOVERY_FILE,
                json.dumps(vector["posix_local_recovery"]).encode("utf-8"),
            )
        return {path: Path(path).read_bytes() for path in self.store.protected_data_files()}

    def assert_unchanged_library(self, original):
        self.assertEqual(self.store._load(self.store.GAMES_FILE), VECTORS["expected_games"])
        self.assertEqual(
            {path: Path(path).read_bytes() for path in self.store.protected_data_files()}, original
        )

    def test_pre_refactor_v1_vault_opens_without_rewriting_data(self):
        before = self.install_vector("v1")
        self.store.unlock_data_vault(VECTORS["password"])
        self.assert_unchanged_library(before)

    def test_pre_refactor_v2_vault_opens_without_rewriting_data(self):
        before = self.install_vector("v2")
        self.store.unlock_data_vault(VECTORS["password"])
        self.assert_unchanged_library(before)

    def test_pre_refactor_v1_migrates_then_resets_without_rewriting_library(self):
        before = self.install_vector("v1")
        self.store.enable_local_password_reset(VECTORS["password"])
        self.store.clear_vault_key()
        self.store.reset_vault_password_locally(NEW_PASSWORD)
        self.store.clear_vault_key()
        self.store.unlock_data_vault(NEW_PASSWORD)
        self.assert_unchanged_library(before)

    @unittest.skipIf(
        os.name == "nt", "Old POSIX recovery fixture is intentionally not portable to Windows"
    )
    def test_pre_refactor_local_recovery_record_still_resets_v2(self):
        before = self.install_vector("v2", local_recovery=True)
        self.store.reset_vault_password_locally(NEW_PASSWORD)
        self.assert_unchanged_library(before)
        with self.assertRaises(self.store.VaultPasswordError):
            self.store.verify_vault_password(VECTORS["password"], self.store.load_vault_metadata())

    def test_windows_recovery_context_has_not_changed(self):
        self.assertEqual(self.store.LOCAL_RECOVERY_ENTROPY, b"XVVIIX_LOCAL_RECOVERY_DPAPI_V1:")
        self.assertEqual(self.store.VAULT_KEY_WRAP_AAD, b"XVVIIX_VAULT_KEY_WRAP_V2:")
        self.assertEqual(self.store.VAULT_AAD, b"XVVIIX_ENCRYPTED_DATA_V1")
