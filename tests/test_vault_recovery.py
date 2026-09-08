"""Data integrity/recovery regression tests; no real libraries are ever opened."""

import base64
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from tests.support import IsolatedLauncherTest, NEW_PASSWORD, PASSWORD


class VaultRecoveryTests(IsolatedLauncherTest):
    def tamper(self, filepath):
        envelope = json.loads(Path(filepath).read_bytes())
        ciphertext = bytearray(base64.b64decode(envelope["ciphertext"]))
        ciphertext[-1] ^= 1
        envelope["ciphertext"] = base64.b64encode(ciphertext).decode("ascii")
        payload = json.dumps(envelope).encode("utf-8")
        self.app.vault._atomic_write_bytes(filepath, payload)
        return payload

    def unlock(self):
        self.app.vault.clear_vault_key()
        self.assertTrue(self.app.vault.unlock_data_vault(PASSWORD))
        self.assertTrue(self.app.load_all_data_files())

    def archives(self):
        return sorted(Path(self.runtime.name).glob("games.json.corrupt-backup-*"))

    def test_damaged_backup_does_not_block_healthy_primary(self):
        item = self.make_vault()
        self.tamper(self.app.vault.GAMES_FILE + ".bak")
        data = self.data_bytes()
        self.unlock()
        self.assertEqual(self.app.games, [item])
        self.assertEqual(self.data_bytes(), data)
        self.assertTrue(
            any("games.json.bak" in warning for warning in self.app.vault.load_warnings)
        )

    def test_truncated_backup_is_kept_and_warned_about(self):
        self.make_vault()
        path = self.app.vault.GAMES_FILE + ".bak"
        self.app.vault._atomic_write_bytes(path, b'{"truncated":')
        self.unlock()
        self.assertEqual(Path(path).read_bytes(), b'{"truncated":')
        self.assertTrue(self.app.vault.load_warnings)

    def test_backup_using_an_unrelated_key_is_not_used(self):
        item = self.make_vault()
        path = self.app.vault.GAMES_FILE + ".bak"
        payload = self.app.vault.encrypt_data_bytes(b"[]", b"z" * 32)
        self.app.vault._atomic_write_bytes(path, payload)
        self.unlock()
        self.assertEqual(self.app.games, [item])
        self.assertEqual(Path(path).read_bytes(), payload)

    def test_damaged_historical_archive_does_not_block_unlock_or_reset(self):
        self.make_vault()
        archive = self.app.vault.GAMES_FILE + ".corrupt-old-fixture"
        self.app.vault._atomic_write_bytes(archive, b"original damaged artifact")
        data = self.data_bytes()
        self.unlock()
        self.app.vault.reset_vault_password_locally(NEW_PASSWORD)
        self.assertEqual(self.data_bytes(), data)
        self.assertEqual(Path(archive).read_bytes(), b"original damaged artifact")

    def test_recovery_warnings_are_deduplicated(self):
        self.make_vault()
        self.tamper(self.app.vault.GAMES_FILE + ".bak")
        self.unlock()
        first = list(self.app.vault.load_warnings)
        self.unlock()
        self.assertEqual(self.app.vault.load_warnings, first)

    def test_current_authentication_failure_blocks_unlock_without_changing_files(self):
        self.make_vault()
        self.tamper(self.app.vault.GAMES_FILE)
        data = self.data_bytes()
        self.app.vault.clear_vault_key()
        with self.assertRaisesRegex(self.app.vault.VaultError, "valid encrypted backup"):
            self.app.vault.unlock_data_vault(PASSWORD)
        self.assertIsNone(self.app.vault.vault_key)
        self.assertEqual(self.data_bytes(), data)

    def test_current_invalid_json_is_not_reencrypted_or_replaced(self):
        self.make_vault()
        self.app.vault._atomic_write_bytes(self.app.vault.GAMES_FILE, b'{"broken":')
        data = self.data_bytes()
        with self.assertRaises(self.app.vault.VaultError):
            self.app.vault.unlock_data_vault(PASSWORD)
        self.assertEqual(self.data_bytes(), data)

    def test_authenticated_nonarray_data_is_rejected(self):
        self.make_vault()
        self.app.vault._atomic_write_bytes(
            self.app.vault.GAMES_FILE,
            self.app.vault.encrypt_data_bytes(b'{"unexpected":true}', self.app.vault.vault_key),
        )
        data = self.data_bytes()
        with self.assertRaisesRegex(self.app.vault.VaultError, "JSON array"):
            self.app.vault.unlock_data_vault(PASSWORD)
        self.assertEqual(self.data_bytes(), data)

    def test_invalid_current_and_backup_still_fail_closed(self):
        self.make_vault()
        self.tamper(self.app.vault.GAMES_FILE)
        self.tamper(self.app.vault.GAMES_FILE + ".bak")
        data = self.data_bytes()
        with self.assertRaisesRegex(self.app.vault.VaultError, "could not be verified either"):
            self.app.vault.unlock_data_vault(PASSWORD)
        self.assertEqual(self.data_bytes(), data)

    def test_migration_checks_every_primary_before_writing_any_of_them(self):
        item = self.make_vault()
        self.app.vault._atomic_write_bytes(self.app.vault.GAMES_FILE, json.dumps([item]).encode())
        self.tamper(self.app.vault.APPS_FILE)
        data = self.data_bytes()
        with self.assertRaises(self.app.vault.VaultError):
            self.app.vault.unlock_data_vault(PASSWORD)
        self.assertEqual(self.data_bytes(), data)

    def test_valid_legacy_primary_and_backup_are_encrypted_on_unlock(self):
        item = self.make_vault()
        for path in (self.app.vault.GAMES_FILE, self.app.vault.GAMES_FILE + ".bak"):
            self.app.vault._atomic_write_bytes(path, json.dumps([item]).encode())
        self.unlock()
        for path in (self.app.vault.GAMES_FILE, self.app.vault.GAMES_FILE + ".bak"):
            self.assertEqual(
                json.loads(Path(path).read_bytes())["format"], self.app.vault.ENCRYPTED_DATA_FORMAT
            )
            self.assertEqual(self.app.vault.read_data_json(path), [item])

    def test_missing_primary_with_backup_is_not_initialized_to_empty(self):
        self.make_vault()
        Path(self.app.vault.GAMES_FILE).unlink()
        backup = Path(self.app.vault.GAMES_FILE + ".bak").read_bytes()
        with self.assertRaisesRegex(self.app.vault.VaultError, "primary file is missing"):
            self.app.vault.unlock_data_vault(PASSWORD)
        self.assertFalse(Path(self.app.vault.GAMES_FILE).exists())
        self.assertEqual(Path(self.app.vault.GAMES_FILE + ".bak").read_bytes(), backup)

    def test_missing_category_without_backup_is_initialized_with_notice(self):
        self.make_vault()
        Path(self.app.vault.APPS_FILE).unlink()
        self.unlock()
        self.assertEqual(self.app.apps, [])
        self.assertEqual(self.app.vault.read_data_json(self.app.vault.APPS_FILE), [])
        self.assertTrue(
            any("apps.json was missing" in warning for warning in self.app.vault.load_warnings)
        )

    def test_reset_will_not_initialize_a_missing_primary(self):
        self.make_vault()
        Path(self.app.vault.APPS_FILE).unlink()
        before = self.settings_bytes()
        with self.assertRaisesRegex(self.app.vault.VaultError, "primary file is missing"):
            self.app.vault.reset_vault_password_locally(NEW_PASSWORD)
        self.assertEqual(self.settings_bytes(), before)
        self.assertFalse(Path(self.app.vault.APPS_FILE).exists())

    def test_direct_loader_does_not_return_empty_data_after_parse_failure(self):
        self.make_vault()
        self.app.vault._atomic_write_bytes(self.app.vault.GAMES_FILE, b"invalid json")
        before = Path(self.app.vault.GAMES_FILE).read_bytes()
        with self.assertRaisesRegex(self.app.vault.VaultError, "empty library was not substituted"):
            self.app.vault._load(self.app.vault.GAMES_FILE)
        self.assertEqual(Path(self.app.vault.GAMES_FILE).read_bytes(), before)

    def test_auxiliary_loader_does_not_return_empty_data_after_schema_failure(self):
        self.make_vault()
        self.app.vault._atomic_write_bytes(
            self.app.vault.REPORTS_FILE,
            self.app.vault.encrypt_data_bytes(b"{}", self.app.vault.vault_key),
        )
        before = Path(self.app.vault.REPORTS_FILE).read_bytes()
        with self.assertRaises(self.app.vault.VaultError):
            self.app.vault._load_auxiliary_records(
                self.app.vault.REPORTS_FILE, self.app.models.normalize_report, 250
            )
        self.assertEqual(Path(self.app.vault.REPORTS_FILE).read_bytes(), before)

    def test_read_failure_does_not_replace_library(self):
        self.make_vault()
        data = self.data_bytes()
        with patch.object(
            self.app.vault, "read_data_json", side_effect=OSError("Injected read failure")
        ):
            with self.assertRaises(self.app.vault.VaultError):
                self.app.vault._load(self.app.vault.GAMES_FILE)
        self.assertEqual(self.data_bytes(), data)

    def test_save_refuses_to_rotate_bad_primary_over_good_backup(self):
        self.make_vault()
        self.tamper(self.app.vault.GAMES_FILE)
        data = self.data_bytes()
        self.assertFalse(self.app.vault._save(self.app.vault.GAMES_FILE, []))
        self.assertEqual(self.data_bytes(), data)

    def test_save_refuses_to_fill_missing_primary_over_existing_backup(self):
        self.make_vault()
        Path(self.app.vault.GAMES_FILE).unlink()
        data = self.data_bytes()
        self.assertFalse(self.app.vault._save(self.app.vault.GAMES_FILE, []))
        self.assertEqual(self.data_bytes(), data)
        self.assertFalse(Path(self.app.vault.GAMES_FILE).exists())

    def test_next_save_preserves_bad_backup_bytes_before_rotation(self):
        item = self.make_vault()
        damaged = self.tamper(self.app.vault.GAMES_FILE + ".bak")
        changed = {**item, "playtime": item["playtime"] + 30}
        self.assertTrue(self.app.vault._save(self.app.vault.GAMES_FILE, [changed]))
        self.assertEqual(len(self.archives()), 1)
        self.assertEqual(self.archives()[0].read_bytes(), damaged)
        self.assertEqual(self.app.vault.read_data_json(self.app.vault.GAMES_FILE + ".bak"), [item])
        self.assertEqual(self.app.vault.read_data_json(self.app.vault.GAMES_FILE), [changed])
        self.unlock()
        self.assertEqual(self.app.games, [changed])

    def test_existing_archive_with_different_content_is_not_overwritten(self):
        item = self.make_vault()
        damaged = self.tamper(self.app.vault.GAMES_FILE + ".bak")
        digest = hashlib.sha256(damaged).hexdigest()[:16]
        occupied = Path(self.app.vault.GAMES_FILE + ".corrupt-backup-" + digest)
        occupied.write_bytes(b"another archived artifact")
        self.assertTrue(self.app.vault._save(self.app.vault.GAMES_FILE, [item]))
        self.assertEqual(occupied.read_bytes(), b"another archived artifact")
        self.assertEqual(len(self.archives()), 2)
        self.assertIn(damaged, [path.read_bytes() for path in self.archives()])

    def test_identical_bad_backup_is_not_archived_repeatedly(self):
        item = self.make_vault()
        damaged = self.tamper(self.app.vault.GAMES_FILE + ".bak")
        self.assertTrue(self.app.vault._save(self.app.vault.GAMES_FILE, [item]))
        self.app.vault._atomic_write_bytes(self.app.vault.GAMES_FILE + ".bak", damaged)
        self.assertTrue(self.app.vault._save(self.app.vault.GAMES_FILE, [item]))
        self.assertEqual(len(self.archives()), 1)

    def test_failed_preservation_blocks_save_and_keeps_originals(self):
        item = self.make_vault()
        self.tamper(self.app.vault.GAMES_FILE + ".bak")
        data = self.data_bytes()
        with patch.object(
            self.app.vault,
            "_preserve_damaged_backup",
            side_effect=OSError("Injected archive failure"),
        ):
            self.assertFalse(self.app.vault._save(self.app.vault.GAMES_FILE, [item]))
        self.assertEqual(self.data_bytes(), data)

    def test_backup_write_failure_does_not_commit_new_primary(self):
        item = self.make_vault()
        data = self.data_bytes()
        original = self.app.vault._atomic_write_bytes

        def fail_backup(path, payload):
            if path == self.app.vault.GAMES_FILE + ".bak":
                raise OSError("Injected backup write failure")
            return original(path, payload)

        with patch.object(self.app.vault, "_atomic_write_bytes", side_effect=fail_backup):
            self.assertFalse(
                self.app.vault._save(self.app.vault.GAMES_FILE, [{**item, "playtime": 999}])
            )
        self.assertEqual(self.data_bytes(), data)
        self.assertFalse(list(Path(self.runtime.name).glob("*.tmp")))

    @unittest.skipIf(os.name == "nt", "POSIX archive permissions")
    def test_preserved_archive_has_private_permissions(self):
        item = self.make_vault()
        self.tamper(self.app.vault.GAMES_FILE + ".bak")
        self.assertTrue(self.app.vault._save(self.app.vault.GAMES_FILE, [item]))
        self.assertEqual(self.archives()[0].stat().st_mode & 0o777, 0o600)

    def test_invalid_legacy_primary_prevents_vault_metadata_commit(self):
        self.app.vault._atomic_write_bytes(self.app.vault.GAMES_FILE, b"[]")
        self.app.vault._atomic_write_bytes(self.app.vault.APPS_FILE, b"broken original data")
        before = self.data_bytes()
        with self.assertRaisesRegex(self.app.vault.VaultError, "Cannot create the vault"):
            self.app.vault.create_data_vault(PASSWORD, allow_local_reset=True)
        self.assertEqual(self.data_bytes(), before)
        self.assertFalse(Path(self.app.vault.VAULT_FILE).exists())
        self.assertFalse(Path(self.app.vault.LOCAL_RECOVERY_FILE).exists())
        self.assertFalse(list(Path(self.runtime.name).glob("*.vault-stage")))

    def test_legacy_opt_in_also_tolerates_damaged_backup(self):
        self.make_vault(recoverable=False)
        self.tamper(self.app.vault.GAMES_FILE + ".bak")
        before = self.data_bytes()
        self.app.vault.enable_local_password_reset(PASSWORD)
        self.assertTrue(self.app.vault.local_password_reset_status()[0])
        self.assertEqual(self.data_bytes(), before)

    def test_runtime_save_failure_notifies_through_ui_queue_only_once(self):
        self.make_vault()
        self.tamper(self.app.vault.GAMES_FILE)
        self.app.data_loaded = True
        self.app.root = object()  # Worker-safe check: no Tk method may be called here.
        with patch.object(self.app, "post_ui") as post:
            self.assertFalse(self.app.vault._save(self.app.vault.GAMES_FILE, []))
            self.assertFalse(self.app.vault._save(self.app.vault.GAMES_FILE, []))
            post.assert_called_once()
            self.assertIs(post.call_args.args[0], self.app.messagebox.showwarning)
            self.assertIn("could not be saved", post.call_args.args[2])

    def test_direct_loaders_refuse_empty_result_when_backup_of_missing_primary_exists(self):
        self.make_vault()
        Path(self.app.vault.GAMES_FILE).unlink()
        before = self.data_bytes()
        with self.assertRaisesRegex(self.app.vault.VaultError, "backup exists"):
            self.app.vault._load(self.app.vault.GAMES_FILE)
        with self.assertRaisesRegex(self.app.vault.VaultError, "backup exists"):
            self.app.vault._load_auxiliary_records(
                self.app.vault.GAMES_FILE, self.app.models.normalize_report, 250
            )
        self.assertEqual(self.data_bytes(), before)

    def test_directory_failure_is_reported_as_failed_save(self):
        self.make_vault()
        before = self.data_bytes()
        with patch.object(
            self.app.os, "makedirs", side_effect=OSError("Injected directory failure")
        ):
            self.assertFalse(self.app.vault._save(self.app.vault.GAMES_FILE, []))
        self.assertEqual(self.data_bytes(), before)

    def test_user_can_restore_verified_backup_without_discarding_bad_original(self):
        item = self.make_vault()
        self.assertTrue(
            self.app.vault._save(self.app.vault.GAMES_FILE, [{**item, "playtime": 999}])
        )
        original = self.tamper(self.app.vault.GAMES_FILE)
        with self.assertRaisesRegex(self.app.vault.VaultError, "valid encrypted backup"):
            self.app.vault.unlock_data_vault(PASSWORD)
        # Simulate the user's explicit, documented file-copy recovery steps.
        preserved = self.app.vault.GAMES_FILE + ".corrupt-manual"
        self.app.vault._atomic_write_bytes(preserved, original)
        self.app.vault._atomic_write_bytes(
            self.app.vault.GAMES_FILE, Path(self.app.vault.GAMES_FILE + ".bak").read_bytes()
        )
        self.unlock()
        self.assertEqual(self.app.games, [item])
        self.assertEqual(Path(preserved).read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
