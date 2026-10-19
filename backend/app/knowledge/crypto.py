from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken

_PREFIX = "fernet:v1:"


class CredentialCipherError(RuntimeError):
    pass


class CredentialCipher:
    def __init__(self, master_key: str) -> None:
        try:
            decoded = base64.b64decode(master_key, altchars=b"-_", validate=True)
            if base64.urlsafe_b64encode(decoded).decode("ascii") != master_key:
                raise ValueError
            self._fernet = Fernet(master_key.encode("ascii"))
        except (TypeError, ValueError):
            raise CredentialCipherError("Credential encryption key is invalid.") from None

    def encrypt(self, value: str) -> str:
        try:
            return _PREFIX + self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
        except (AttributeError, UnicodeError):
            raise CredentialCipherError("Credential encryption failed.") from None

    def decrypt(self, token: str) -> str:
        if not isinstance(token, str) or not token.startswith(_PREFIX) or token == _PREFIX:
            raise CredentialCipherError("Credential ciphertext format is invalid.")
        try:
            return self._fernet.decrypt(token[len(_PREFIX) :].encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError):
            raise CredentialCipherError("Credential decryption failed.") from None
