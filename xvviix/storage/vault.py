"""VaultStore owns one data directory, encryption session and integrity policy.

No GUI imports. The application supplies a shared lock and optional warning callbacks.
"""

import base64
import glob
import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import uuid
from ..models import normalize_item
from ..security import crypto
from ..utils import record_timestamp


class VaultStore:
    """An independent vault instance; constructing one never writes user data."""

    VAULT_FORMAT = crypto.VAULT_FORMAT
    RECOVERABLE_VAULT_FORMAT = crypto.RECOVERABLE_VAULT_FORMAT
    SUPPORTED_VAULT_FORMATS = crypto.SUPPORTED_VAULT_FORMATS
    LOCAL_RECOVERY_FORMAT = crypto.LOCAL_RECOVERY_FORMAT
    LOCAL_RECOVERY_IS_WINDOWS = crypto.LOCAL_RECOVERY_IS_WINDOWS
    VAULT_KEY_WRAP_AAD = crypto.VAULT_KEY_WRAP_AAD
    LOCAL_RECOVERY_KEY_CHECK = crypto.LOCAL_RECOVERY_KEY_CHECK
    LOCAL_RECOVERY_ENTROPY = crypto.LOCAL_RECOVERY_ENTROPY
    ENCRYPTED_DATA_FORMAT = crypto.ENCRYPTED_DATA_FORMAT
    VAULT_AAD = crypto.VAULT_AAD
    VAULT_VERIFIER_MESSAGE = crypto.VAULT_VERIFIER_MESSAGE
    VAULT_SCRYPT_N = crypto.VAULT_SCRYPT_N
    VAULT_SCRYPT_R = crypto.VAULT_SCRYPT_R
    VAULT_SCRYPT_P = crypto.VAULT_SCRYPT_P
    HAS_CRYPTOGRAPHY = crypto.HAS_CRYPTOGRAPHY
    VaultError = crypto.VaultError
    VaultPasswordError = crypto.VaultPasswordError
    _decode_base64 = staticmethod(crypto._decode_base64)
    derive_vault_key = staticmethod(crypto.derive_vault_key)
    create_vault_metadata = staticmethod(crypto.create_vault_metadata)
    verify_vault_password = staticmethod(crypto.verify_vault_password)
    _recoverable_vault_id = staticmethod(crypto._recoverable_vault_id)
    _local_recovery_key_check = staticmethod(crypto._local_recovery_key_check)
    create_recoverable_vault_metadata = staticmethod(crypto.create_recoverable_vault_metadata)
    _verify_recovered_data_key = staticmethod(crypto._verify_recovered_data_key)
    _unwrap_vault_data_key = staticmethod(crypto._unwrap_vault_data_key)
    _dpapi_local_recovery = staticmethod(crypto._dpapi_local_recovery)
    encrypt_data_bytes = staticmethod(crypto.encrypt_data_bytes)
    decrypt_data_envelope = staticmethod(crypto.decrypt_data_envelope)
    _parse_json_bytes = staticmethod(crypto._parse_json_bytes)
    _is_encrypted_payload = staticmethod(crypto._is_encrypted_payload)

    def __init__(self, directory, *, lock=None, logger=None, warning_sink=None, runtime_ready=None):
        self.BASE_DIR = os.path.abspath(os.fspath(directory))
        self.data_lock = lock if lock is not None else threading.RLock()
        self.LOG = logger if logger is not None else logging.getLogger("xvviix_launcher")
        self._warning_sink = warning_sink
        self._runtime_ready = runtime_ready if runtime_ready is not None else lambda: False
        self.GAMES_FILE = os.path.join(self.BASE_DIR, "games.json")
        self.APPS_FILE = os.path.join(self.BASE_DIR, "apps.json")
        self.FOUNDED_FILE = os.path.join(self.BASE_DIR, "founded.json")
        self.REPORTS_FILE = os.path.join(self.BASE_DIR, "reports.json")
        self.ACTIVITY_FILE = os.path.join(self.BASE_DIR, "activity.json")
        self.VAULT_FILE = os.path.join(self.BASE_DIR, "xvviix_vault.json")
        self.LOCAL_RECOVERY_FILE = os.path.join(self.BASE_DIR, "xvviix_local_recovery.json")
        self.vault_key = None
        self.vault_enabled = os.path.isfile(self.VAULT_FILE)
        self.load_warnings = []

    def protected_data_files(self):
        """Return user-data JSON files whose payload must be encrypted at rest."""
        return (
            self.GAMES_FILE,
            self.APPS_FILE,
            self.FOUNDED_FILE,
            self.REPORTS_FILE,
            self.ACTIVITY_FILE,
        )

    def is_protected_data_file(self, filepath):
        target = os.path.abspath(filepath)
        return any(target == os.path.abspath(path) for path in self.protected_data_files())

    def _make_local_recovery_payload(self, metadata, key):
        identifier = self._recoverable_vault_id(metadata)
        self._verify_recovered_data_key(key, metadata)
        material = (
            self._dpapi_local_recovery(key, identifier) if self.LOCAL_RECOVERY_IS_WINDOWS else key
        )
        record = {
            "format": self.LOCAL_RECOVERY_FORMAT,
            "vault_id": identifier,
            "protection": "windows-dpapi" if self.LOCAL_RECOVERY_IS_WINDOWS else "file-permissions",
            "key_material": base64.b64encode(material).decode("ascii"),
        }
        return (json.dumps(record, indent=2, ensure_ascii=True) + "\n").encode("utf-8")

    def _load_local_recovery_key(self, metadata):
        if (
            metadata.get("format") != self.RECOVERABLE_VAULT_FORMAT
            or metadata.get("local_password_reset") is not True
        ):
            raise self.VaultError(
                "Password reset was not enabled for this vault. Sign in once with your existing "
                "password and select 'Enable local password reset'. An already-forgotten password "
                "cannot be bypassed for an older vault. Your files have not been changed."
            )
        identifier = self._recoverable_vault_id(metadata)
        try:
            if os.path.getsize(self.LOCAL_RECOVERY_FILE) > 65536:
                raise self.VaultError("The local recovery file exceeds the safety limit")
            with open(self.LOCAL_RECOVERY_FILE, "r", encoding="utf-8") as handle:
                record = json.load(handle)
        except FileNotFoundError as exc:
            raise self.VaultError(
                "The local recovery file is missing. Restore xvviix_local_recovery.json from "
                "your own backup, or sign in with your password to enable reset again. "
                "No password or library data has been changed."
            ) from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise self.VaultError(
                "The local recovery file cannot be read. Sign in with your password to enable reset again."
            ) from exc
        if (
            not isinstance(record, dict)
            or record.get("format") != self.LOCAL_RECOVERY_FORMAT
            or record.get("vault_id") != identifier
        ):
            raise self.VaultError(
                "The local recovery file is damaged or belongs to a different vault"
            )
        material = self._decode_base64(
            record.get("key_material", ""), "local recovery key", 1, 65536
        )
        protection = record.get("protection")
        if protection == "windows-dpapi" and self.LOCAL_RECOVERY_IS_WINDOWS:
            key = self._dpapi_local_recovery(material, identifier, decrypt=True)
        elif protection == "file-permissions" and not self.LOCAL_RECOVERY_IS_WINDOWS:
            if os.stat(self.LOCAL_RECOVERY_FILE).st_mode & 0o077:
                raise self.VaultError(
                    "Local recovery file permissions are too open. Sign in with your password "
                    "and enable local reset again to recreate a private recovery file."
                )
            key = material
        else:
            raise self.VaultError(
                "This recovery file uses protection from another platform. Sign in with your "
                "password and enable reset on this computer."
            )
        return self._verify_recovered_data_key(key, metadata)

    def local_password_reset_status(self):
        """Return availability without unlocking libraries or exposing key material."""
        try:
            self._load_local_recovery_key(self.load_vault_metadata())
            return True, "Local password reset is available on this computer."
        except (self.VaultError, OSError, ValueError) as exc:
            return False, str(exc)

    def _validate_reset_data_key(self, key):
        """Require valid current data; retain but do not trust damaged old artifacts."""
        self._validate_primary_data_files(key)
        self._inspect_auxiliary_data_files(key, migrate_plaintext=False)

    def _commit_password_configuration(self, metadata, recovery_payload=None):
        """Commit backup before primary; roll back ordinary I/O failures, never rewrite libraries.

        The data key is unchanged during both migration and password reset, so an
        interruption between metadata writes does not invalidate library ciphertext.
        """
        payload = (json.dumps(metadata, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
        writes = [] if recovery_payload is None else [(self.LOCAL_RECOVERY_FILE, recovery_payload)]
        writes.extend([(f"{self.VAULT_FILE}.bak", payload), (self.VAULT_FILE, payload)])
        previous = {}
        for filepath, _payload in writes:
            if os.path.exists(filepath):
                if os.path.getsize(filepath) > 65536:
                    raise self.VaultError("Existing password settings exceed the safety limit")
                with open(filepath, "rb") as handle:
                    previous[filepath] = handle.read()
            else:
                previous[filepath] = None
        attempted = []
        try:
            for filepath, content in writes:
                attempted.append(filepath)
                self._atomic_write_bytes(filepath, content)
        except OSError as exc:
            rollback_failed = False
            for filepath in reversed(attempted):
                try:
                    if previous[filepath] is None:
                        if os.path.exists(filepath):
                            os.remove(filepath)
                    else:
                        self._atomic_write_bytes(filepath, previous[filepath])
                except OSError:
                    rollback_failed = True
                    self.LOG.error("Could not restore password settings file: %s", filepath)
            detail = (
                "Keep all vault and recovery files; settings recovery is required."
                if rollback_failed
                else "Previous password settings were restored."
            )
            raise self.VaultError(
                f"Could not save password settings. {detail} Library data was not rewritten."
            ) from exc

    def enable_local_password_reset(self, password):
        """Explicit opt-in after password verification, including migration of v1 vaults."""
        with self.data_lock:
            previous = self.load_vault_metadata()
            key = self.verify_vault_password(password, previous)
            self._validate_reset_data_key(key)
            identifier = (
                self._recoverable_vault_id(previous)
                if previous.get("format") == self.RECOVERABLE_VAULT_FORMAT
                else None
            )
            metadata, key = self.create_recoverable_vault_metadata(password, key, identifier)
            metadata["created"] = previous.get("created", metadata["created"])
            recovery_payload = self._make_local_recovery_payload(metadata, key)
            self._commit_password_configuration(metadata, recovery_payload)
            self.vault_key = key
            self.vault_enabled = True
        return True

    def reset_vault_password_locally(self, new_password):
        """No old password/code: access to the local recovery key authorizes this reset.

        This is a convenience lock, not a password-only security boundary. It never
        creates an empty replacement vault or discards existing libraries/backups.
        """
        if not isinstance(new_password, str) or not 8 <= len(new_password) <= 1024:
            raise self.VaultPasswordError(
                "The new password must contain between 8 and 1024 characters"
            )
        with self.data_lock:
            previous = self.load_vault_metadata()
            key = self._load_local_recovery_key(previous)
            self._validate_reset_data_key(key)
            metadata, key = self.create_recoverable_vault_metadata(
                new_password,
                key,
                self._recoverable_vault_id(previous),
            )
            metadata["created"] = previous.get("created", metadata["created"])
            metadata["password_updated"] = record_timestamp()
            self._commit_password_configuration(metadata)
            self.vault_key = key
            self.vault_enabled = True
        return True

    def read_data_json(self, filepath):
        with open(filepath, "rb") as handle:
            payload = handle.read()
        parsed = self._parse_json_bytes(payload)
        if isinstance(parsed, dict) and parsed.get("format") == self.ENCRYPTED_DATA_FORMAT:
            if self.vault_key is None:
                raise self.VaultError("The data vault is locked")
            plaintext = self.decrypt_data_envelope(parsed, self.vault_key)
            return self._parse_json_bytes(plaintext)
        return parsed

    def _atomic_write_bytes(self, filepath, payload):
        directory = os.path.dirname(filepath) or "."
        os.makedirs(directory, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{os.path.basename(filepath)}.",
            suffix=".tmp",
            dir=directory,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, filepath)
            temporary = ""
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.remove(temporary)
                except OSError:
                    pass

    def _write_vault_metadata(self, metadata):
        payload = (json.dumps(metadata, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
        self._atomic_write_bytes(self.VAULT_FILE, payload)
        try:
            shutil.copy2(self.VAULT_FILE, f"{self.VAULT_FILE}.bak")
        except OSError as exc:
            self.LOG.warning("Could not create vault metadata backup: %s", exc)

    def load_vault_metadata(self):
        failures = []
        for candidate in (self.VAULT_FILE, f"{self.VAULT_FILE}.bak"):
            if not os.path.isfile(candidate):
                continue
            try:
                if os.path.getsize(candidate) > 64 * 1024:
                    raise self.VaultError("Vault metadata exceeds the safety limit")
                with open(candidate, "r", encoding="utf-8") as handle:
                    metadata = json.load(handle)
                if (
                    not isinstance(metadata, dict)
                    or metadata.get("format") not in self.SUPPORTED_VAULT_FORMATS
                ):
                    raise self.VaultError("Unsupported vault metadata")
                if candidate != self.VAULT_FILE:
                    self._atomic_write_bytes(
                        self.VAULT_FILE,
                        (json.dumps(metadata, indent=2, ensure_ascii=True) + "\n").encode("utf-8"),
                    )
                    self.LOG.warning("Restored vault metadata from its backup")
                return metadata
            except (OSError, UnicodeError, json.JSONDecodeError, self.VaultError) as exc:
                failures.append(f"{os.path.basename(candidate)}: {exc}")
        raise self.VaultError("Vault metadata is missing or damaged. " + " | ".join(failures))

    def protected_artifact_paths(self):
        paths = []
        for primary in self.protected_data_files():
            paths.append(primary)
            paths.extend(glob.glob(f"{primary}.bak"))
            paths.extend(glob.glob(f"{primary}.corrupt-*"))
        return list(dict.fromkeys(paths))

    def encrypted_data_exists_without_vault(self):
        marker = self.ENCRYPTED_DATA_FORMAT.encode("ascii")
        for filepath in self.protected_artifact_paths():
            if not os.path.isfile(filepath):
                continue
            try:
                with open(filepath, "rb") as handle:
                    if marker in handle.read(4096):
                        return True
            except OSError:
                continue
        return False

    def _read_validated_data_file(self, filepath, key, allow_plaintext=False):
        """Validate both AEAD integrity and the JSON array shape before trusting a file."""
        with open(filepath, "rb") as handle:
            payload = handle.read()
        parsed = self._parse_json_bytes(payload)
        if isinstance(parsed, dict) and parsed.get("format") == self.ENCRYPTED_DATA_FORMAT:
            records = self._parse_json_bytes(self.decrypt_data_envelope(parsed, key))
            legacy_plaintext = False
        elif allow_plaintext and isinstance(parsed, list):
            records = parsed
            legacy_plaintext = True
        else:
            raise self.VaultError(
                "Expected an encrypted JSON array; the file may be damaged or use an unsupported format"
            )
        if not isinstance(records, list):
            raise self.VaultError("Decrypted data must be a JSON array")
        return payload, legacy_plaintext

    def _vault_recovery_warning(self, message):
        """Record a warning once; presentation is delegated to the optional caller."""
        if message not in self.load_warnings:
            self.load_warnings.append(message)
            self.LOG.warning(message)
            if self._warning_sink is not None:
                self._warning_sink(message)

    def _primary_data_error(self, filepath, reason, key):
        backup = f"{filepath}.bak"
        hint = "Restore this file from your own matching backup before continuing."
        if os.path.isfile(backup):
            try:
                self._read_validated_data_file(backup, key, allow_plaintext=False)
                hint = (
                    f"A valid encrypted backup exists at {os.path.basename(backup)}. "
                    "Close XVVIIX, keep a separate copy of any damaged original, and copy "
                    "the .bak file back to the original filename. Nothing is restored automatically."
                )
            except (self.VaultError, OSError, UnicodeError, json.JSONDecodeError):
                hint = "Its .bak file could not be verified either. Restore your own matching backup before continuing."
        return self.VaultError(
            f"Could not verify {os.path.basename(filepath)}: {reason}. {hint} No library was replaced with empty data."
        )

    def _validate_primary_data_files(self, key, allow_plaintext=False, initialize_missing=False):
        """Validate every primary before returning any migration/initialization writes."""
        pending = []
        for filepath in self.protected_data_files():
            if not os.path.exists(filepath):
                # Never hide the loss of a primary by ignoring an existing backup.
                if not initialize_missing or os.path.exists(f"{filepath}.bak"):
                    raise self._primary_data_error(filepath, "the primary file is missing", key)
                pending.append((filepath, self.encrypt_data_bytes(b"[]\n", key), True))
                continue
            try:
                payload, legacy_plaintext = self._read_validated_data_file(
                    filepath, key, allow_plaintext
                )
            except (self.VaultError, OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise self._primary_data_error(filepath, exc, key) from exc
            if legacy_plaintext:
                pending.append((filepath, self.encrypt_data_bytes(payload, key), False))
        return pending

    def _inspect_auxiliary_data_files(self, key, migrate_plaintext=False):
        """Bad backups/archives are non-fatal, kept untouched, and never treated as data."""
        primaries = set(self.protected_data_files())
        for filepath in self.protected_artifact_paths():
            if filepath in primaries or not os.path.exists(filepath):
                continue
            try:
                payload, legacy_plaintext = self._read_validated_data_file(
                    filepath, key, migrate_plaintext
                )
                if legacy_plaintext:
                    self._atomic_write_bytes(filepath, self.encrypt_data_bytes(payload, key))
            except (self.VaultError, OSError, UnicodeError, json.JSONDecodeError) as exc:
                self._vault_recovery_warning(
                    f"Ignored unverified backup/archive {os.path.basename(filepath)}: {exc}. "
                    "The current data is valid; this file was not changed or used for recovery."
                )

    def _preserve_damaged_backup(self, filepath):
        """Keep exact bytes before normal backup rotation; never overwrite an archive."""
        with open(filepath, "rb") as handle:
            payload = handle.read()
        prefix = filepath[:-4] if filepath.endswith(".bak") else filepath
        digest = hashlib.sha256(payload).hexdigest()[:16]
        for attempt in range(4):
            suffix = digest if attempt == 0 else f"{digest}-{uuid.uuid4().hex[:8]}"
            archive = f"{prefix}.corrupt-backup-{suffix}"
            try:
                descriptor = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                with open(archive, "rb") as handle:
                    if handle.read() == payload:
                        return archive
                continue
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                try:
                    os.remove(archive)
                except OSError:
                    pass
                raise
            return archive
        raise self.VaultError(
            "Could not preserve the damaged backup without overwriting an existing archive"
        )

    def ensure_protected_files_encrypted(self):
        if self.vault_key is None:
            raise self.VaultError("The data vault is locked")
        pending = self._validate_primary_data_files(
            self.vault_key,
            allow_plaintext=True,
            initialize_missing=True,
        )
        for filepath, payload, was_missing in pending:
            self._atomic_write_bytes(filepath, payload)
            if was_missing:
                self._vault_recovery_warning(
                    f"{os.path.basename(filepath)} was missing and had no .bak file. "
                    "An empty encrypted file was initialized; restore your own backup if data was expected."
                )
        self._inspect_auxiliary_data_files(self.vault_key, migrate_plaintext=True)

    def create_data_vault(self, password, allow_local_reset=False):
        """Stage every ciphertext before committing vault metadata and data files."""
        if not self.HAS_CRYPTOGRAPHY:
            raise self.VaultError("Install the cryptography package before creating the vault")
        if os.path.exists(self.VAULT_FILE) or os.path.exists(f"{self.VAULT_FILE}.bak"):
            raise self.VaultError("A data vault already exists; restart XVVIIX and unlock it")
        if os.path.exists(self.LOCAL_RECOVERY_FILE):
            raise self.VaultError(
                "An existing local recovery file was found. Restore its matching vault metadata before setup; it will not be overwritten."
            )
        if allow_local_reset:
            metadata, key = self.create_recoverable_vault_metadata(password)
            recovery_payload = self._make_local_recovery_payload(metadata, key)
        else:
            metadata, key = self.create_vault_metadata(password)
            recovery_payload = None
        staged = []
        try:
            existing = self.protected_artifact_paths()
            targets = list(dict.fromkeys(list(self.protected_data_files()) + existing))
            for filepath in targets:
                if os.path.isfile(filepath):
                    with open(filepath, "rb") as handle:
                        plaintext = handle.read()
                    if self._is_encrypted_payload(plaintext):
                        raise self.VaultError(
                            "Encrypted data already exists. Restore the matching vault metadata before setup."
                        )
                    if filepath in self.protected_data_files():
                        try:
                            if not isinstance(self._parse_json_bytes(plaintext), list):
                                raise ValueError("data root must be a JSON array")
                        except (ValueError, UnicodeError) as exc:
                            raise self.VaultError(
                                f"Cannot create the vault: {os.path.basename(filepath)} is not valid library data. "
                                "The existing files were not changed."
                            ) from exc
                else:
                    plaintext = b"[]\n"
                payload = self.encrypt_data_bytes(plaintext, key)
                directory = os.path.dirname(filepath) or "."
                os.makedirs(directory, exist_ok=True)
                descriptor, temporary = tempfile.mkstemp(
                    prefix=f".{os.path.basename(filepath)}.",
                    suffix=".vault-stage",
                    dir=directory,
                )
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                staged.append((temporary, filepath))

            self._write_vault_metadata(metadata)
            self.vault_key = key
            self.vault_enabled = True
            for temporary, filepath in staged:
                os.replace(temporary, filepath)
            staged.clear()
            self.ensure_protected_files_encrypted()
            if recovery_payload is not None:
                # mkstemp in _atomic_write_bytes creates a private (0600) POSIX file.
                self._atomic_write_bytes(self.LOCAL_RECOVERY_FILE, recovery_payload)
            return True
        finally:
            for temporary, _filepath in staged:
                try:
                    if os.path.exists(temporary):
                        os.remove(temporary)
                except OSError:
                    pass

    def unlock_data_vault(self, password):
        metadata = self.load_vault_metadata()
        key = self.verify_vault_password(password, metadata)
        self.vault_key = key
        self.vault_enabled = True
        try:
            self.ensure_protected_files_encrypted()
        except Exception:
            self.vault_key = None
            raise
        return True

    def clear_vault_key(self):
        self.vault_key = None

    def _load_auxiliary_records(self, filepath, normalizer, limit):
        if not os.path.exists(filepath):
            if os.path.exists(f"{filepath}.bak"):
                raise self.VaultError(
                    f"{os.path.basename(filepath)} is missing but its backup exists. "
                    "An empty library was not substituted; restore the matching backup first."
                )
            return []
        try:
            raw_data = self.read_data_json(filepath)
            if not isinstance(raw_data, list):
                raise ValueError("data root must be a JSON array")
            result = []
            for index, raw in enumerate(raw_data[:limit]):
                try:
                    result.append(normalizer(raw))
                except ValueError as exc:
                    self.LOG.warning("Skipped invalid entry %s in %s: %s", index, filepath, exc)
            result.sort(key=lambda record: record.get("epoch", 0.0), reverse=True)
            return result
        except (OSError, json.JSONDecodeError, UnicodeError, ValueError, self.VaultError) as exc:
            message = (
                f"Could not load {os.path.basename(filepath)}: {exc}. "
                "The original file was left unchanged; an empty library was not substituted."
            )
            self.LOG.error(message)
            raise self.VaultError(message) from exc

    def _load(self, filepath):
        if not os.path.exists(filepath):
            if os.path.exists(f"{filepath}.bak"):
                raise self.VaultError(
                    f"{os.path.basename(filepath)} is missing but its backup exists. "
                    "An empty library was not substituted; restore the matching backup first."
                )
            return []
        try:
            raw_data = self.read_data_json(filepath)
            if not isinstance(raw_data, list):
                raise ValueError("library root must be a JSON array")
            result = []
            for index, raw in enumerate(raw_data):
                try:
                    result.append(normalize_item(raw))
                except ValueError as exc:
                    self.LOG.warning("Skipped invalid entry %s in %s: %s", index, filepath, exc)
            return result
        except (OSError, json.JSONDecodeError, UnicodeError, ValueError, self.VaultError) as exc:
            message = (
                f"Could not load {os.path.basename(filepath)}: {exc}. "
                "The original file was left unchanged; an empty library was not substituted."
            )
            self.LOG.error(message)
            raise self.VaultError(message) from exc

    def _save(self, filepath, data):
        """Save atomically, never rotating unverified data over the last good backup."""
        directory = os.path.dirname(filepath) or "."
        temp_path = None
        with self.data_lock:
            try:
                os.makedirs(directory, exist_ok=True)
                protected = self.is_protected_data_file(filepath)
                plaintext = (json.dumps(data, indent=4, ensure_ascii=False) + "\n").encode("utf-8")
                if protected:
                    if not self.vault_enabled or self.vault_key is None:
                        raise self.VaultError(
                            "Refused to write protected data while the vault is locked"
                        )
                    if not isinstance(data, list):
                        raise self.VaultError("Protected data must be a JSON array")
                    payload = self.encrypt_data_bytes(plaintext, self.vault_key)
                else:
                    payload = plaintext

                current_payload = None
                backup = f"{filepath}.bak"
                if os.path.exists(filepath):
                    if protected:
                        # A failed load must never turn into an empty save over real data.
                        current_payload, _legacy = self._read_validated_data_file(
                            filepath, self.vault_key
                        )
                    else:
                        with open(filepath, "rb") as handle:
                            current_payload = handle.read()
                    if protected and os.path.exists(backup):
                        try:
                            self._read_validated_data_file(backup, self.vault_key)
                        except (self.VaultError, OSError, UnicodeError, json.JSONDecodeError):
                            archive = self._preserve_damaged_backup(backup)
                            self._vault_recovery_warning(
                                f"Preserved damaged backup {os.path.basename(backup)} as "
                                f"{os.path.basename(archive)} before creating a healthy backup."
                            )
                elif protected and os.path.exists(backup):
                    raise self._primary_data_error(
                        filepath, "the primary file is missing", self.vault_key
                    )

                descriptor, temp_path = tempfile.mkstemp(
                    prefix=f".{os.path.basename(filepath)}.",
                    suffix=".tmp",
                    dir=directory,
                )
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                if current_payload is not None:
                    self._atomic_write_bytes(backup, current_payload)
                os.replace(temp_path, filepath)
                temp_path = None
                return True
            except (OSError, TypeError, ValueError, self.VaultError) as exc:
                self.LOG.error("Could not save %s: %s", filepath, exc)
                if self._runtime_ready():
                    self._vault_recovery_warning(
                        f"Changes to {os.path.basename(filepath)} could not be saved: {exc}. "
                        "Do not discard your original files; resolve the error and retry saving."
                    )
                return False
            finally:
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
