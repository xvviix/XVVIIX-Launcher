"""Version-compatible vault cryptography. No Tk, application state or file I/O."""

import base64
import ctypes
from ctypes import wintypes
from datetime import datetime
import hashlib
import hmac
import json
import os
import uuid
from ..utils import record_timestamp

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    HAS_CRYPTOGRAPHY = True
except (ImportError, OSError):
    InvalidTag = None
    AESGCM = None
    HAS_CRYPTOGRAPHY = False

VAULT_FORMAT = "xvviix-vault-v1"

RECOVERABLE_VAULT_FORMAT = "xvviix-vault-v2"

SUPPORTED_VAULT_FORMATS = (VAULT_FORMAT, RECOVERABLE_VAULT_FORMAT)

LOCAL_RECOVERY_FORMAT = "xvviix-local-recovery-v1"

LOCAL_RECOVERY_IS_WINDOWS = os.name == "nt"

VAULT_KEY_WRAP_AAD = b"XVVIIX_VAULT_KEY_WRAP_V2:"

LOCAL_RECOVERY_KEY_CHECK = b"XVVIIX_LOCAL_RECOVERY_KEY_V1:"

LOCAL_RECOVERY_ENTROPY = b"XVVIIX_LOCAL_RECOVERY_DPAPI_V1:"

ENCRYPTED_DATA_FORMAT = "xvviix-encrypted-json-v1"

VAULT_AAD = b"XVVIIX_ENCRYPTED_DATA_V1"

VAULT_VERIFIER_MESSAGE = b"XVVIIX_VAULT_PASSWORD_VERIFIER_V1"

VAULT_SCRYPT_N = 32768

VAULT_SCRYPT_R = 8

VAULT_SCRYPT_P = 1


class VaultError(Exception):
    pass


class VaultPasswordError(VaultError):
    pass


def _decode_base64(value, label, minimum=1, maximum=1024 * 1024 * 64):
    try:
        decoded = base64.b64decode(str(value).encode("ascii"), validate=True)
    except (ValueError, UnicodeError) as exc:
        raise VaultError(f"Invalid {label} encoding") from exc
    if not minimum <= len(decoded) <= maximum:
        raise VaultError(f"Invalid {label} length")
    return decoded


def derive_vault_key(password, metadata):
    if not isinstance(password, str) or not password or len(password) > 1024:
        raise VaultPasswordError("Invalid password")
    if not isinstance(metadata, dict) or metadata.get("format") not in SUPPORTED_VAULT_FORMATS:
        raise VaultError("Unsupported or damaged vault metadata")
    kdf = metadata.get("kdf")
    if not isinstance(kdf, dict) or kdf.get("name") != "scrypt":
        raise VaultError("Unsupported password derivation format")
    try:
        n = int(kdf.get("n"))
        r = int(kdf.get("r"))
        p = int(kdf.get("p"))
    except (TypeError, ValueError) as exc:
        raise VaultError("Damaged password derivation settings") from exc
    if n < 16384 or n > 131072 or n & (n - 1) or not 1 <= r <= 16 or not 1 <= p <= 4:
        raise VaultError("Unsafe password derivation settings were rejected")
    salt = _decode_base64(kdf.get("salt", ""), "vault salt", 16, 64)
    try:
        return hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=32,
            maxmem=128 * 1024 * 1024,
        )
    except (ValueError, OSError, UnicodeError) as exc:
        raise VaultError(f"Password derivation failed: {exc}") from exc


def create_vault_metadata(password):
    salt = os.urandom(16)
    metadata = {
        "format": VAULT_FORMAT,
        "cipher": "AES-256-GCM",
        "kdf": {
            "name": "scrypt",
            "n": VAULT_SCRYPT_N,
            "r": VAULT_SCRYPT_R,
            "p": VAULT_SCRYPT_P,
            "salt": base64.b64encode(salt).decode("ascii"),
        },
        "created": record_timestamp()
        if "record_timestamp" in globals()
        else datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    key = derive_vault_key(password, metadata)
    metadata["verifier"] = base64.b64encode(
        hmac.new(key, VAULT_VERIFIER_MESSAGE, hashlib.sha256).digest()
    ).decode("ascii")
    return metadata, key


def verify_vault_password(password, metadata):
    key = derive_vault_key(password, metadata)
    expected = _decode_base64(metadata.get("verifier", ""), "password verifier", 32, 32)
    actual = hmac.new(key, VAULT_VERIFIER_MESSAGE, hashlib.sha256).digest()
    if not hmac.compare_digest(actual, expected):
        raise VaultPasswordError("Incorrect password")
    if metadata.get("format") == RECOVERABLE_VAULT_FORMAT:
        return _unwrap_vault_data_key(metadata, key)
    return key


def _recoverable_vault_id(metadata):
    identifier = metadata.get("vault_id", "")
    if (
        not isinstance(identifier, str)
        or len(identifier) != 32
        or any(char not in "0123456789abcdef" for char in identifier)
    ):
        raise VaultError("Invalid local recovery vault identifier")
    return identifier


def _local_recovery_key_check(key, identifier):
    return hmac.new(
        key,
        LOCAL_RECOVERY_KEY_CHECK + identifier.encode("ascii"),
        hashlib.sha256,
    ).digest()


def create_recoverable_vault_metadata(password, data_key=None, identifier=None):
    """Wrap a stable data key; changing the password never rewrites library data."""
    if not HAS_CRYPTOGRAPHY or AESGCM is None:
        raise VaultError("The cryptography package is not installed")
    key = os.urandom(32) if data_key is None else data_key
    if not isinstance(key, bytes) or len(key) != 32:
        raise VaultError("Invalid vault data key")
    metadata, password_key = create_vault_metadata(password)
    metadata.update(
        {
            "format": RECOVERABLE_VAULT_FORMAT,
            "vault_id": identifier if identifier is not None else uuid.uuid4().hex,
            "local_password_reset": True,
        }
    )
    identifier = _recoverable_vault_id(metadata)
    nonce = os.urandom(12)
    ciphertext = AESGCM(password_key).encrypt(
        nonce,
        key,
        VAULT_KEY_WRAP_AAD + identifier.encode("ascii"),
    )
    metadata["wrapped_data_key"] = {
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    metadata["data_key_check"] = base64.b64encode(
        _local_recovery_key_check(key, identifier),
    ).decode("ascii")
    return metadata, key


def _verify_recovered_data_key(key, metadata):
    if not isinstance(key, bytes) or len(key) != 32:
        raise VaultError("Invalid local recovery key")
    expected = _decode_base64(metadata.get("data_key_check", ""), "data key check", 32, 32)
    actual = _local_recovery_key_check(key, _recoverable_vault_id(metadata))
    if not hmac.compare_digest(actual, expected):
        raise VaultError(
            "The local recovery key does not match this vault. No password was changed."
        )
    return key


def _unwrap_vault_data_key(metadata, password_key):
    if not HAS_CRYPTOGRAPHY or AESGCM is None:
        raise VaultError("The cryptography package is not installed")
    identifier = _recoverable_vault_id(metadata)
    wrapped = metadata.get("wrapped_data_key")
    if not isinstance(wrapped, dict):
        raise VaultError("The wrapped vault key is missing or damaged")
    nonce = _decode_base64(wrapped.get("nonce", ""), "wrapped key nonce", 12, 12)
    ciphertext = _decode_base64(wrapped.get("ciphertext", ""), "wrapped key", 48, 48)
    try:
        key = AESGCM(password_key).decrypt(
            nonce,
            ciphertext,
            VAULT_KEY_WRAP_AAD + identifier.encode("ascii"),
        )
    except InvalidTag as exc:
        raise VaultError("Vault key authentication failed") from exc
    return _verify_recovered_data_key(key, metadata)


def _dpapi_local_recovery(payload, identifier, decrypt=False):
    """Protect a recovery key for this Windows user; never fall back to plaintext."""
    if not LOCAL_RECOVERY_IS_WINDOWS:
        raise VaultError("Windows account protection is not available on this platform")

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    source_buffer = ctypes.create_string_buffer(payload)
    source = DataBlob(len(payload), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy_bytes = LOCAL_RECOVERY_ENTROPY + identifier.encode("ascii")
    entropy_buffer = ctypes.create_string_buffer(entropy_bytes)
    entropy = DataBlob(
        len(entropy_bytes), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte))
    )
    destination = DataBlob()
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
        function.argtypes = [
            ctypes.POINTER(DataBlob),
            ctypes.c_void_p if decrypt else wintypes.LPCWSTR,
            ctypes.POINTER(DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(DataBlob),
        ]
        function.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        # CRYPTPROTECT_UI_FORBIDDEN; protection is per-user, not machine-wide.
        success = function(
            ctypes.byref(source),
            None if decrypt else "XVVIIX local password reset",
            ctypes.byref(entropy),
            None,
            None,
            0x01,
            ctypes.byref(destination),
        )
        try:
            if not success or not destination.pbData or not 1 <= destination.cbData <= 65536:
                raise VaultError(
                    "Windows could not access the local recovery key for this account. "
                    "Sign in with your master password to enable reset on this computer."
                )
            return ctypes.string_at(destination.pbData, destination.cbData)
        finally:
            if destination.pbData:
                kernel32.LocalFree(ctypes.cast(destination.pbData, ctypes.c_void_p))
    except (AttributeError, OSError) as exc:
        raise VaultError(
            "Windows local recovery protection is unavailable; no unprotected key was saved."
        ) from exc


def encrypt_data_bytes(plaintext, key):
    if not HAS_CRYPTOGRAPHY or AESGCM is None:
        raise VaultError("The cryptography package is not installed")
    if not isinstance(plaintext, bytes) or not isinstance(key, bytes) or len(key) != 32:
        raise VaultError("Invalid encryption input")
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, VAULT_AAD)
    envelope = {
        "format": ENCRYPTED_DATA_FORMAT,
        "cipher": "AES-256-GCM",
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    return (json.dumps(envelope, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def decrypt_data_envelope(envelope, key):
    if not HAS_CRYPTOGRAPHY or AESGCM is None:
        raise VaultError("The cryptography package is not installed")
    if not isinstance(envelope, dict) or envelope.get("format") != ENCRYPTED_DATA_FORMAT:
        raise VaultError("Unsupported encrypted data format")
    nonce = _decode_base64(envelope.get("nonce", ""), "encryption nonce", 12, 12)
    ciphertext = _decode_base64(
        envelope.get("ciphertext", ""),
        "encrypted payload",
        16,
        256 * 1024 * 1024,
    )
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, VAULT_AAD)
    except InvalidTag as exc:
        raise VaultError("Encrypted data authentication failed") from exc
    except (ValueError, TypeError) as exc:
        raise VaultError(f"Encrypted data could not be opened: {exc}") from exc


def _parse_json_bytes(payload):
    return json.loads(payload.decode("utf-8"))


def _is_encrypted_payload(payload):
    try:
        parsed = _parse_json_bytes(payload)
    except (UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(parsed, dict) and parsed.get("format") == ENCRYPTED_DATA_FORMAT
