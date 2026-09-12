import os
from pathlib import Path

from cryptography.fernet import Fernet

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
ENV_VAR = "KITE_TERMINAL_ENCRYPTION_KEY"


def ensure_encryption_key() -> None:
    """Generate and persist a Fernet key into .env if one isn't already configured.

    Must run before any encrypt()/decrypt() call and before account seeding.
    """
    if os.environ.get(ENV_VAR):
        return

    key = Fernet.generate_key().decode()
    with open(ENV_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n{ENV_VAR}={key}\n")
    os.environ[ENV_VAR] = key
    print(f"[crypto] Generated a new {ENV_VAR} and saved it to {ENV_PATH}")


def _get_fernet() -> Fernet:
    return Fernet(os.environ[ENV_VAR].encode())


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode()).decode()


def encrypt_optional(plaintext: str | None) -> str | None:
    return encrypt(plaintext) if plaintext is not None else None


def decrypt_optional(ciphertext: str | None) -> str | None:
    return decrypt(ciphertext) if ciphertext is not None else None
