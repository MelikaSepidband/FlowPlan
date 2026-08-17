"""Opaque base64 offset tokens for result paging.

Tokens are URL-safe base64 encodings of ``offset:<n>``. They are opaque to
agents: tools that support paging accept a ``page_token`` parameter and
decode it back to an integer offset with :func:`decode_token`.
"""

import base64
import binascii

__all__ = ["encode_token", "decode_token"]

_PREFIX = "offset:"


def encode_token(offset: int) -> str:
    """Encode a non-negative integer offset as an opaque token string."""
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError(f"offset must be a non-negative int, got {offset!r}")
    raw = f"{_PREFIX}{offset}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_token(token: str | None) -> int:
    """Decode an opaque page token back to an integer offset.

    ``None`` and the empty string decode to offset 0. Malformed tokens raise
    ``ValueError``.
    """
    if token is None or token == "":
        return 0
    try:
        raw = base64.b64decode(token.encode("ascii"), altchars=b"-_", validate=True)
        text = raw.decode("ascii")
    except (binascii.Error, UnicodeDecodeError, UnicodeEncodeError, ValueError) as exc:
        raise ValueError(f"invalid page token: {token!r}") from exc
    if not text.startswith(_PREFIX):
        raise ValueError(f"invalid page token: {token!r}")
    try:
        offset = int(text[len(_PREFIX):])
    except ValueError as exc:
        raise ValueError(f"invalid page token: {token!r}") from exc
    if offset < 0:
        raise ValueError(f"invalid page token: {token!r}")
    return offset
