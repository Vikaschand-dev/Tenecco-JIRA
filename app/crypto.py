import base64
import hashlib
import logging
from functools import lru_cache

from cryptography.fernet import Fernet

from app.config import get_settings

log = logging.getLogger(__name__)


@lru_cache
def _fernet() -> Fernet:
    settings = get_settings()
    key = settings.token_encryption_key
    if not key:
        # Development fallback: derive a key from the session secret so the app runs
        # out of the box. Always set TOKEN_ENCRYPTION_KEY in production.
        log.warning("TOKEN_ENCRYPTION_KEY not set; deriving one from SESSION_SECRET (dev only)")
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.session_secret.encode()).digest()).decode()
    return Fernet(key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()
