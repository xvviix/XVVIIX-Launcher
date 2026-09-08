"""Password reset/migration tests. All passwords and vaults are disposable fixtures."""

import base64
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from tests.support import IsolatedLauncherTest, NEW_PASSWORD, PASSWORD


class PasswordResetTests(IsolatedLauncherTest):
    @staticmethod
    def corrupt_field(record, field):
        altered = dict(record)
        data = bytearray(base64.b64decode(altered[field]))
        data[-1] ^= 1
        altered[field] = base64.b64encode(data).decode("ascii")
        return altered

    def write_record(self, path, record):
        self.app.vault._atomic_write_bytes(path, json.dumps(record).encode("utf-8"))

    def assert_password(self, password):
        self.app.vault.clear_vault_key()
        self.assertTrue(self.app.vault.unlock_data_vault(password))
        self.assertTrue(self.app.load_all_data_files())

    def test_legacy_creation_remains_supported_without_local_key(self):
        self.make_vault(recoverable=False)
        self.assertEqual(self.app.vault.load_vault_metadata()["format"], "xvviix-vault-v1")
        self.assertFalse(Path(self.app.vault.LOCAL_RECOVERY_FILE).exists())
        self.assert_password(PASSWORD)

    def test_new_recoverable_vault_keeps_password_out_of_files(self):
        item = self.make_vault()
        metadata = self.app.vault.load_vault_metadata()
        self.assertEqual(metadata["format"], "xvviix-vault-v2")
        self.assertTrue(metadata["local_password_reset"])
        for content in list(self.settings_bytes().values()) + list(self.data_bytes().values()):
            self.assertNotIn(PASSWORD.encode(), content)
            self.assertNotIn(item["name"].encode(), content)
        self.assert_password(PASSWORD)
        self.assertEqual(self.app.games, [item])

    def test_reset_without_old_password_preserves_all_data_and_recovery_file(self):
        item = self.make_vault()
        data = self.data_bytes()
        recovery = Path(self.app.vault.LOCAL_RECOVERY_FILE).read_bytes()
        key = self.app.vault.vault_key
        self.app.vault.clear_vault_key()
        self.assertTrue(self.app.vault.reset_vault_password_locally(NEW_PASSWORD))
        self.assertEqual(self.app.vault.vault_key, key)
        self.assertEqual(self.data_bytes(), data)
        self.assertEqual(Path(self.app.vault.LOCAL_RECOVERY_FILE).read_bytes(), recovery)
        for path in (self.app.vault.VAULT_FILE, self.app.vault.VAULT_FILE + ".bak"):
            metadata = json.loads(Path(path).read_bytes())
            with self.assertRaises(self.app.vault.VaultPasswordError):
                self.app.vault.verify_vault_password(PASSWORD, metadata)
            self.assertEqual(self.app.vault.verify_vault_password(NEW_PASSWORD, metadata), key)
        self.assert_password(NEW_PASSWORD)
        self.assertEqual(self.app.games, [item])

    def test_repeated_resets_keep_stable_data_key_and_vault_identity(self):
        self.make_vault()
        identifier = self.app.vault.load_vault_metadata()["vault_id"]
        data = self.data_bytes()
        self.app.vault.reset_vault_password_locally(NEW_PASSWORD)
        second_password = "another-test-password-پسورد"
        self.app.vault.clear_vault_key()
        self.app.vault.reset_vault_password_locally(second_password)
        self.assertEqual(self.app.vault.load_vault_metadata()["vault_id"], identifier)
        self.assertEqual(self.data_bytes(), data)
        self.assert_password(second_password)
        for password in (PASSWORD, NEW_PASSWORD):
            with self.assertRaises(self.app.vault.VaultPasswordError):
                self.app.vault.verify_vault_password(password, self.app.vault.load_vault_metadata())

    def test_legacy_opt_in_migrates_metadata_without_reencrypting_data(self):
        item = self.make_vault(recoverable=False)
        data = self.data_bytes()
        old_key = self.app.vault.vault_key
        self.assertTrue(self.app.vault.enable_local_password_reset(PASSWORD))
        self.assertEqual(self.app.vault.load_vault_metadata()["format"], "xvviix-vault-v2")
        self.assertEqual(self.app.vault.vault_key, old_key)
        self.assertEqual(self.data_bytes(), data)
        self.assert_password(PASSWORD)
        self.app.vault.clear_vault_key()
        self.app.vault.reset_vault_password_locally(NEW_PASSWORD)
        self.assert_password(NEW_PASSWORD)
        self.assertEqual(self.app.games, [item])

    def test_legacy_vault_cannot_reset_without_prior_opt_in(self):
        self.make_vault(recoverable=False)
        before = self.settings_bytes()
        self.app.vault.clear_vault_key()
        with self.assertRaisesRegex(self.app.vault.VaultError, "not enabled"):
            self.app.vault.reset_vault_password_locally(NEW_PASSWORD)
        self.assertEqual(self.settings_bytes(), before)
        self.assertFalse(self.app.vault.local_password_reset_status()[0])
        self.assert_password(PASSWORD)

    def test_opt_in_requires_current_password(self):
        self.make_vault(recoverable=False)
        before = self.settings_bytes()
        with self.assertRaises(self.app.vault.VaultPasswordError):
            self.app.vault.enable_local_password_reset("a-wrong-test-password")
        self.assertEqual(self.settings_bytes(), before)

    def test_normal_sign_in_does_not_silently_enable_recovery(self):
        self.make_vault(recoverable=False)
        self.assert_password(PASSWORD)
        self.assertFalse(Path(self.app.vault.LOCAL_RECOVERY_FILE).exists())
        self.assertEqual(self.app.vault.load_vault_metadata()["format"], "xvviix-vault-v1")

    def test_missing_recovery_file_does_not_use_cached_unlocked_key(self):
        self.make_vault()
        Path(self.app.vault.LOCAL_RECOVERY_FILE).unlink()
        before = self.settings_bytes()
        with self.assertRaisesRegex(self.app.vault.VaultError, "missing"):
            self.app.vault.reset_vault_password_locally(NEW_PASSWORD)
        self.assertEqual(self.settings_bytes(), before)
        self.assert_password(PASSWORD)

    def test_recovery_file_can_be_recreated_after_password_sign_in(self):
        self.make_vault()
        data = self.data_bytes()
        identifier = self.app.vault.load_vault_metadata()["vault_id"]
        Path(self.app.vault.LOCAL_RECOVERY_FILE).unlink()
        self.app.vault.enable_local_password_reset(PASSWORD)
        self.assertTrue(self.app.vault.local_password_reset_status()[0])
        self.assertEqual(self.app.vault.load_vault_metadata()["vault_id"], identifier)
        self.assertEqual(self.data_bytes(), data)
        self.app.vault.reset_vault_password_locally(NEW_PASSWORD)
        self.assert_password(NEW_PASSWORD)

    def test_malformed_recovery_file_leaves_password_settings_unchanged(self):
        self.make_vault()
        self.app.vault._atomic_write_bytes(self.app.vault.LOCAL_RECOVERY_FILE, b"{broken-json")
        before = self.settings_bytes()
        with self.assertRaisesRegex(self.app.vault.VaultError, "cannot be read"):
            self.app.vault.reset_vault_password_locally(NEW_PASSWORD)
        self.assertEqual(self.settings_bytes(), before)

    def test_oversized_recovery_file_is_rejected(self):
        self.make_vault()
        self.app.vault._atomic_write_bytes(self.app.vault.LOCAL_RECOVERY_FILE, b"x" * 65537)
        before = self.settings_bytes()
        with self.assertRaisesRegex(self.app.vault.VaultError, "safety limit"):
            self.app.vault.reset_vault_password_locally(NEW_PASSWORD)
        self.assertEqual(self.settings_bytes(), before)

    def test_recovery_file_from_another_vault_is_rejected(self):
        self.make_vault()
        record = json.loads(Path(self.app.vault.LOCAL_RECOVERY_FILE).read_bytes())
        record["vault_id"] = "0" * 32
        self.write_record(self.app.vault.LOCAL_RECOVERY_FILE, record)
        before = self.settings_bytes()
        with self.assertRaisesRegex(self.app.vault.VaultError, "different vault"):
            self.app.vault.reset_vault_password_locally(NEW_PASSWORD)
        self.assertEqual(self.settings_bytes(), before)

    @unittest.skipIf(os.name == "nt", "POSIX raw recovery-key binding check")
    def test_tampered_local_key_fails_even_with_an_empty_library(self):
        self.app.vault.create_data_vault(PASSWORD, allow_local_reset=True)
        record = json.loads(Path(self.app.vault.LOCAL_RECOVERY_FILE).read_bytes())
        self.write_record(
            self.app.vault.LOCAL_RECOVERY_FILE, self.corrupt_field(record, "key_material")
        )
        before = self.settings_bytes()
        with self.assertRaisesRegex(self.app.vault.VaultError, "does not match"):
            self.app.vault.reset_vault_password_locally(NEW_PASSWORD)
        self.assertEqual(self.settings_bytes(), before)

    def test_authenticated_data_corruption_aborts_before_password_change(self):
        self.make_vault()
        envelope = json.loads(Path(self.app.vault.GAMES_FILE).read_bytes())
        self.write_record(self.app.vault.GAMES_FILE, self.corrupt_field(envelope, "ciphertext"))
        before = self.settings_bytes()
        data = self.data_bytes()
        with self.assertRaisesRegex(self.app.vault.VaultError, "Could not verify games.json"):
            self.app.vault.reset_vault_password_locally(NEW_PASSWORD)
        self.assertEqual(self.settings_bytes(), before)
        self.assertEqual(self.data_bytes(), data)

    def test_corrupt_backup_is_preserved_without_blocking_password_reset(self):
        self.make_vault()
        path = self.app.vault.GAMES_FILE + ".bak"
        envelope = json.loads(Path(path).read_bytes())
        self.write_record(path, self.corrupt_field(envelope, "ciphertext"))
        data = self.data_bytes()
        self.app.vault.clear_vault_key()
        self.assertTrue(self.app.vault.reset_vault_password_locally(NEW_PASSWORD))
        self.assertEqual(self.data_bytes(), data)
        self.assertTrue(
            any("games.json.bak" in warning for warning in self.app.vault.load_warnings)
        )
        self.assert_password(NEW_PASSWORD)

    def test_recovery_can_repair_damaged_password_key_wrap(self):
        self.make_vault()
        metadata = self.app.vault.load_vault_metadata()
        metadata["wrapped_data_key"] = self.corrupt_field(
            metadata["wrapped_data_key"], "ciphertext"
        )
        self.write_record(self.app.vault.VAULT_FILE, metadata)
        with self.assertRaisesRegex(self.app.vault.VaultError, "authentication failed"):
            self.app.vault.verify_vault_password(PASSWORD, metadata)
        self.app.vault.reset_vault_password_locally(NEW_PASSWORD)
        self.assert_password(NEW_PASSWORD)

    def test_invalid_new_passwords_do_not_write_files(self):
        self.make_vault()
        before = self.settings_bytes()
        for password in (None, "", "short", "x" * 1025):
            with self.subTest(length=None if password is None else len(password)):
                with self.assertRaises(self.app.vault.VaultPasswordError):
                    self.app.vault.reset_vault_password_locally(password)
                self.assertEqual(self.settings_bytes(), before)

    def fail_primary_write_once(self):
        original = self.app.vault._atomic_write_bytes
        failed = False

        def write(path, payload):
            nonlocal failed
            if path == self.app.vault.VAULT_FILE and not failed:
                failed = True
                raise OSError("Injected settings write failure")
            return original(path, payload)

        return patch.object(self.app.vault, "_atomic_write_bytes", side_effect=write)

    def test_failed_reset_restores_original_metadata_and_backup(self):
        self.make_vault()
        before = self.settings_bytes()
        data = self.data_bytes()
        self.app.vault.clear_vault_key()
        with (
            self.fail_primary_write_once(),
            self.assertRaisesRegex(self.app.vault.VaultError, "restored"),
        ):
            self.app.vault.reset_vault_password_locally(NEW_PASSWORD)
        self.assertIsNone(self.app.vault.vault_key)
        self.assertEqual(self.settings_bytes(), before)
        self.assertEqual(self.data_bytes(), data)
        self.assert_password(PASSWORD)

    def test_failed_legacy_opt_in_removes_only_newly_created_recovery_file(self):
        self.make_vault(recoverable=False)
        before = self.settings_bytes()
        data = self.data_bytes()
        with (
            self.fail_primary_write_once(),
            self.assertRaisesRegex(self.app.vault.VaultError, "restored"),
        ):
            self.app.vault.enable_local_password_reset(PASSWORD)
        self.assertEqual(self.settings_bytes(), before)
        self.assertEqual(self.data_bytes(), data)
        self.assert_password(PASSWORD)

    def test_failed_reenable_restores_existing_recovery_file(self):
        self.make_vault()
        before = self.settings_bytes()
        with self.fail_primary_write_once(), self.assertRaises(self.app.vault.VaultError):
            self.app.vault.enable_local_password_reset(PASSWORD)
        self.assertEqual(self.settings_bytes(), before)
        self.assertTrue(self.app.vault.local_password_reset_status()[0])

    def test_metadata_backup_after_reset_uses_new_password(self):
        self.make_vault()
        self.app.vault.reset_vault_password_locally(NEW_PASSWORD)
        Path(self.app.vault.VAULT_FILE).unlink()
        self.app.vault.clear_vault_key()
        self.assert_password(NEW_PASSWORD)
        with self.assertRaises(self.app.vault.VaultPasswordError):
            self.app.vault.verify_vault_password(PASSWORD, self.app.vault.load_vault_metadata())

    def test_existing_orphan_recovery_file_is_never_overwritten_by_setup(self):
        original = b"orphaned-recovery-fixture"
        self.app.vault._atomic_write_bytes(self.app.vault.LOCAL_RECOVERY_FILE, original)
        with self.assertRaisesRegex(self.app.vault.VaultError, "will not be overwritten"):
            self.app.vault.create_data_vault(PASSWORD, allow_local_reset=True)
        self.assertEqual(Path(self.app.vault.LOCAL_RECOVERY_FILE).read_bytes(), original)
        self.assertFalse(Path(self.app.vault.VAULT_FILE).exists())

    @unittest.skipIf(os.name == "nt", "POSIX file permissions")
    def test_posix_local_recovery_is_private(self):
        self.make_vault()
        self.assertEqual(Path(self.app.vault.LOCAL_RECOVERY_FILE).stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.app.vault.local_password_reset_status()[0])

    @unittest.skipIf(os.name == "nt", "POSIX file permissions")
    def test_open_file_permissions_require_password_to_reenable(self):
        self.make_vault()
        Path(self.app.vault.LOCAL_RECOVERY_FILE).chmod(0o644)
        with self.assertRaisesRegex(self.app.vault.VaultError, "permissions are too open"):
            self.app.vault.reset_vault_password_locally(NEW_PASSWORD)
        self.app.vault.enable_local_password_reset(PASSWORD)
        self.assertEqual(Path(self.app.vault.LOCAL_RECOVERY_FILE).stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.app.vault.local_password_reset_status()[0])

    def test_windows_protection_failure_never_falls_back_to_raw_key(self):
        with (
            patch.object(self.app.vault, "LOCAL_RECOVERY_IS_WINDOWS", True),
            patch.object(
                self.app.vault,
                "_dpapi_local_recovery",
                side_effect=self.app.vault.VaultError("DPAPI unavailable"),
            ),
            self.assertRaisesRegex(self.app.vault.VaultError, "DPAPI unavailable"),
        ):
            self.app.vault.create_data_vault(PASSWORD, allow_local_reset=True)
        self.assertFalse(Path(self.app.vault.LOCAL_RECOVERY_FILE).exists())
        self.assertFalse(Path(self.app.vault.VAULT_FILE).exists())
        self.assertEqual(self.data_bytes(), {})

    def test_platform_protection_is_not_silently_downgraded(self):
        self.make_vault()
        record = json.loads(Path(self.app.vault.LOCAL_RECOVERY_FILE).read_bytes())
        record["protection"] = "file-permissions" if os.name == "nt" else "windows-dpapi"
        self.write_record(self.app.vault.LOCAL_RECOVERY_FILE, record)
        before = self.settings_bytes()
        with self.assertRaisesRegex(self.app.vault.VaultError, "another platform"):
            self.app.vault.reset_vault_password_locally(NEW_PASSWORD)
        self.assertEqual(self.settings_bytes(), before)

    @unittest.skipUnless(os.name == "nt", "Requires a real Windows DPAPI user profile")
    def test_windows_dpapi_integration_roundtrip(self):
        self.make_vault()
        record = json.loads(Path(self.app.vault.LOCAL_RECOVERY_FILE).read_bytes())
        self.assertEqual(record["protection"], "windows-dpapi")
        self.assertNotEqual(base64.b64decode(record["key_material"]), self.app.vault.vault_key)
        self.app.vault.clear_vault_key()
        self.app.vault.reset_vault_password_locally(NEW_PASSWORD)
        self.assert_password(NEW_PASSWORD)


if __name__ == "__main__":
    unittest.main()
