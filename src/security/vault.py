"""
Encrypted credential storage for the Hyperliquid trading bot.

Uses Fernet (AES-128-CBC with HMAC-SHA256) from the cryptography library
for symmetric encryption of API keys and sensitive configuration.

The encryption key is derived from either:
  1. A user-provided password (PBKDF2-HMAC-SHA256)
  2. The OS keyring (system credential store) as a secure fallback

If the vault is not initialised, values fall back to environment variables.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import weakref
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VAULT_PATH: Path = Path(__file__).resolve().parents[2] / "data" / "vault.enc"
SALT_LENGTH: int = 32
ITERATIONS: int = 480_000
MAX_KEY_NAME_LEN: int = 128
MAX_VALUE_LEN: int = 4096
_KEY_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_\-\.]+$", re.ASCII)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class VaultError(Exception):
    """Base exception for vault operations."""


class VaultNotInitializedError(VaultError):
    """Raised when the vault file does not exist and no env fallback is available."""


class VaultIntegrityError(VaultError):
    """Raised when the vault payload has been tampered with or is corrupted."""


class VaultKeyError(VaultError):
    """Raised when a requested key does not exist in the vault."""


# ---------------------------------------------------------------------------
# Key derivation helpers
# ---------------------------------------------------------------------------

def _derive_key(password: str, salt: bytes) -> bytes:
    """Derive a Fernet-compatible 32-byte key from *password* and *salt*."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def _key_from_keyring(service: str = "hyperliquid_bot_vault") -> Optional[bytes]:
    """
    Attempt to retrieve an existing encryption key from the OS keyring.
    Returns ``None`` if the keyring module is unavailable or no key exists.
    """
    try:
        import keyring

        stored = keyring.get_password(service, "master")
        if stored is None:
            return None
        return stored.encode("utf-8")
    except Exception:  # noqa: BLE001
        return None


def _save_key_to_keyring(key: bytes, service: str = "hyperliquid_bot_vault") -> None:
    """Persist *key* into the OS keyring under *service*."""
    try:
        import keyring

        keyring.set_password(service, "master", key.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to store vault key in OS keyring: %s", exc)


def _generate_master_key(password: Optional[str] = None) -> tuple[bytes, bytes]:
    """
    Generate or retrieve a master encryption key.

    If *password* is provided, a random salt is generated and a key is
    derived via PBKDF2.  If *password* is ``None``, the OS keyring is
    checked first; if empty, a random 32-byte key is created and stored
    automatically.

    Returns ``(key, salt)`` where *salt* may be empty when the keyring
    is used exclusively.
    """
    if password is not None:
        salt = os.urandom(SALT_LENGTH)
        key = _derive_key(password, salt)
        return key, salt

    kr_key = _key_from_keyring()
    if kr_key is not None:
        return kr_key, b""

    # No password, no keyring entry → auto-generate a random key
    raw_key = os.urandom(32)
    key = base64.urlsafe_b64encode(raw_key)
    _save_key_to_keyring(key)
    return key, b""


# ---------------------------------------------------------------------------
# Validation helpers (ReDoS-safe — fixed-length limits)
# ---------------------------------------------------------------------------

def _validate_key_name(key_name: str) -> None:
    """Validate *key_name* format and length."""
    if not isinstance(key_name, str):
        raise VaultError("key_name must be a string")
    if not (1 <= len(key_name) <= MAX_KEY_NAME_LEN):
        raise VaultError(
            f"key_name length must be between 1 and {MAX_KEY_NAME_LEN}"
        )
    if not _KEY_NAME_PATTERN.match(key_name):
        raise VaultError(
            "key_name contains invalid characters. Allowed: A-Z, a-z, 0-9, _, -, ."
        )


def _validate_value(value: str) -> None:
    """Validate *value* type and length."""
    if not isinstance(value, str):
        raise VaultError("value must be a string")
    if len(value) > MAX_VALUE_LEN:
        raise VaultError(f"value exceeds maximum length of {MAX_VALUE_LEN}")


# ---------------------------------------------------------------------------
# Vault
# ---------------------------------------------------------------------------

class Vault:
    """
    Encrypted credential vault backed by a single JSON blob encrypted with
    Fernet (AES-128-CBC + HMAC-SHA256).

    The vault file format (plaintext before encryption) is::

        {
          "salt": "<base64>",           # only present for password-derived keys
          "created_at": <unix_timestamp>,
          "entries": {
            "key_name": "plain_value",
            ...
          }
        }

    Example::

        vault = Vault(password="my_strong_password")
        vault.store("hyperliquid_api_key", "abc123")
        print(vault.retrieve("hyperliquid_api_key"))
    """

    def __init__(
        self,
        password: Optional[str] = None,
        vault_path: Optional[Union[str, Path]] = None,
    ) -> None:
        """
        Initialise the vault.

        Args:
            password: User-provided password to derive the encryption key.
                If ``None``, the OS keyring is consulted, and if still empty
                a random key is generated and stored in the keyring.
            vault_path: Override the default ``data/vault.enc`` path.
        """
        self._vault_path: Path = (
            Path(vault_path) if vault_path is not None else VAULT_PATH
        )
        self._key: bytes
        self._salt: bytes
        self._key, self._salt = _generate_master_key(password)
        self._fernet: Fernet = Fernet(self._key)
        self._entries: Dict[str, str] = {}
        self._loaded: bool = False
        self._lock = threading.Lock()  # HIGH-005: thread-safe mutation

        # HIGH-006: Register cleanup to zero key on object destruction
        weakref.finalize(self, self._zero_key, self._key)

        if self._vault_path.exists():
            self._load()
        else:
            self._save()  # Create an empty encrypted vault file

    @staticmethod
    def _zero_key(key_ref: bytes) -> None:
        """Overwrite key bytes in memory (best-effort on GC)."""
        try:
            for i in range(len(key_ref)):
                key_ref[i] = 0
        except (TypeError, IndexError):
            pass  # bytes are immutable in Python — best effort only

    # -- Internal persistence ------------------------------------------------

    def _load(self) -> None:
        """Decrypt and load entries from the vault file on disk."""
        with self._lock:
            if not self._vault_path.exists():
                self._entries = {}
                self._loaded = True
                return

            raw = self._vault_path.read_bytes()
            if not raw:
                self._entries = {}
                self._loaded = True
                return

            try:
                decrypted: bytes = self._fernet.decrypt(raw)
            except InvalidToken as exc:
                raise VaultIntegrityError(
                    "Vault decryption failed — wrong password or corrupted data"
                ) from exc

            try:
                payload: Dict[str, Any] = json.loads(decrypted.decode("utf-8"))

                # HIGH-009: JSON depth limit to prevent DoS
                def _check_depth(obj, depth=0):
                    if depth > 20:
                        raise VaultIntegrityError("Vault JSON exceeds maximum nesting depth")
                    if isinstance(obj, dict):
                        for v in obj.values():
                            _check_depth(v, depth + 1)
                    elif isinstance(obj, list):
                        for v in obj:
                            _check_depth(v, depth + 1)

                _check_depth(payload)

            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise VaultIntegrityError("Vault payload is not valid JSON") from exc

            # Optional integrity check: stored salt must match ours when present
            stored_salt_b64: Optional[str] = payload.get("salt")
            if stored_salt_b64 is not None:
                stored_salt = base64.b64decode(stored_salt_b64)
                if stored_salt != self._salt:
                    raise VaultIntegrityError("Vault salt mismatch — wrong password?")

            entries = payload.get("entries")
            if not isinstance(entries, dict):
                raise VaultIntegrityError("Vault entries block is malformed")

            # Validate every loaded key/value so corrupt payloads fail fast
            for k, v in entries.items():
                _validate_key_name(k)
                _validate_value(v)

            self._entries = {str(k): str(v) for k, v in entries.items()}
            self._loaded = True
            logger.debug("Vault loaded from %s", self._vault_path)

    def _save(self) -> None:
        """Encrypt and persist the current entries to disk (atomic write)."""
        with self._lock:
            payload: Dict[str, Any] = {
                "created_at": int(time.time()),
                "entries": self._entries,
            }
            if self._salt:
                payload["salt"] = base64.b64encode(self._salt).decode("ascii")

            plaintext = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
            encrypted: bytes = self._fernet.encrypt(plaintext)

            # Atomic write via temp file + rename
            self._vault_path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(
                dir=str(self._vault_path.parent),
                prefix=".vault_tmp_",
            )
            try:
                os.write(fd, encrypted)
                os.close(fd)
                shutil.move(temp_path, self._vault_path)
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                Path(temp_path).unlink(missing_ok=True)
                raise

            logger.debug("Vault saved to %s (%d entries)", self._vault_path, len(self._entries))

    # -- Public API ----------------------------------------------------------

    def store(self, key_name: str, value: str) -> None:
        """
        Encrypt and store *value* under *key_name*.

        Args:
            key_name: Identifier for the secret (alphanumeric + ``_-.``).
            value: Plain-text secret to encrypt.

        Raises:
            VaultError: If validation fails.
        """
        _validate_key_name(key_name)
        _validate_value(value)
        with self._lock:
            self._entries[key_name] = value
            self._save()

    def retrieve(self, key_name: str, fallback_env: bool = True) -> str:
        """
        Retrieve a decrypted value by *key_name*.

        If the vault is not initialised or the key is missing, the method
        falls back to the equivalent upper-cased environment variable
        (e.g. ``hyperliquid_api_key`` → ``HYPERLIQUID_API_KEY``) when
        *fallback_env* is ``True``.

        Args:
            key_name: Key to look up.
            fallback_env: Whether to check environment variables on miss.

        Returns:
            The stored plain-text value.

        Raises:
            VaultKeyError: If the key is absent and no env fallback exists.
            VaultNotInitializedError: If the vault file does not exist.
        """
        _validate_key_name(key_name)

        if not self._vault_path.exists():
            if fallback_env:
                env_val = os.environ.get(key_name.upper().replace(".", "_"))
                if env_val is not None:
                    return env_val
            raise VaultNotInitializedError(
                f"Vault not initialised and no env fallback for '{key_name}'"
            )

        with self._lock:
            if key_name in self._entries:
                return self._entries[key_name]

        if fallback_env:
            env_val = os.environ.get(key_name.upper().replace(".", "_"))
            if env_val is not None:
                return env_val

        raise VaultKeyError(f"Key '{key_name}' not found in vault or environment")

    def delete(self, key_name: str) -> bool:
        """
        Remove *key_name* from the vault.

        Returns:
            ``True`` if the key existed and was removed, ``False`` otherwise.
        """
        _validate_key_name(key_name)
        with self._lock:
            if key_name in self._entries:
                del self._entries[key_name]
                self._save()
                return True
        return False

    def list_keys(self) -> List[str]:
        """Return a sorted list of all key names currently stored."""
        with self._lock:
            return sorted(self._entries.keys())

    def rotate(self, new_password: Optional[str] = None) -> None:
        """
        Re-encrypt the vault with a new master key.

        This creates a fresh salt and derived key, then atomically rewrites
        the vault file. The old key is discarded.
        """
        with self._lock:
            self._key, self._salt = _generate_master_key(new_password)
            self._fernet = Fernet(self._key)
            self._save()
        logger.info("Vault key rotated successfully")

    # -- Environment-variable helpers -----------------------------------------

    def import_from_env(self, *key_names: str, overwrite: bool = False) -> int:
        """
        Copy secrets from environment variables into the vault.

        Args:
            key_names: Keys to import (upper-cased env names are tried).
            overwrite: If ``False``, existing vault keys are skipped.

        Returns:
            Number of keys imported.
        """
        imported = 0
        with self._lock:
            for key_name in key_names:
                _validate_key_name(key_name)
                if not overwrite and key_name in self._entries:
                    continue
                env_val = os.environ.get(key_name.upper().replace(".", "_"))
                if env_val is not None:
                    self._entries[key_name] = env_val
                    imported += 1
            if imported:
                self._save()
        return imported

    def exists(self) -> bool:
        """Return whether the vault file exists on disk."""
        return self._vault_path.exists()


# ---------------------------------------------------------------------------
# Convenience singleton (lazy, thread-safe)
# ---------------------------------------------------------------------------

_vault_singleton: Optional[Vault] = None
_vault_singleton_lock = threading.Lock()


def get_vault(
    password: Optional[str] = None,
    vault_path: Optional[Union[str, Path]] = None,
) -> Vault:
    """
    Return the lazily-initialised global vault instance (thread-safe).

    Callers should pass *password* on first invocation; subsequent calls
    may omit it and receive the same instance.
    """
    global _vault_singleton
    with _vault_singleton_lock:
        if _vault_singleton is None:
            _vault_singleton = Vault(password=password, vault_path=vault_path)
        return _vault_singleton


def reset_vault_singleton() -> None:
    """Clear the global vault singleton (useful in tests)."""
    global _vault_singleton
    with _vault_singleton_lock:
        _vault_singleton = None
