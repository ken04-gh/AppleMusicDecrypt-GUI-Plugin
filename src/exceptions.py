class DecryptException(Exception):
    ...


class NotTimeSyncedLyricsException(Exception):
    ...


class CodecNotFoundException(Exception):
    ...


class RetryableDecryptException(Exception):
    ...


class SongNotPassIntegrityCheckException(Exception):
    ...


class LoginCancelledException(Exception):
    """User cancelled Apple ID login or 2FA."""


class TwoFAResendException(Exception):
    """User requested a new 2FA verification code."""
