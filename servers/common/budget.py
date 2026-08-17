"""Token-budget estimation and truncation for MCP tool results.

The study-wide hard output budget is TOKEN_BUDGET = 4000 tokens per tool
call (STUDY_PLAN.md section 2). Token counts use tiktoken's o200k_base
encoding when it is available, falling back to a length-based estimate
(serialized length // 4) when tiktoken cannot be loaded or fails to encode.

Compaction (the ``_compact`` study conditions): an item-level shrink that
drops null/empty fields, abbreviates the common verbose keys to 1-3 char
aliases, and truncates long free-text fields (signatures, docstrings,
code lines). Measured on real LinuxFLBench envelopes this removes ~34-43%
of MCP-result tokens (the bloat is pretty-print indentation + repeated
verbose keys + untruncated signatures). The envelope itself is then emitted
as a SEPARATOR-compact JSON string (no indentation) so FastMCP passes it
through verbatim instead of re-pretty-printing it with indent=2.
"""

import json
from typing import Any

__all__ = [
    "TOKEN_BUDGET",
    "estimate_tokens",
    "fit",
    "serialize",
    "compact_item",
    "compact_serialize",
    "KEY_ALIASES",
]

TOKEN_BUDGET: int = 4000

#: Verbose envelope keys -> short aliases used in compact mode. These are the
#: high-frequency keys across the structural servers' item dicts; the agent
#: reads them as anchors, so 1-3 char names cost far fewer tokens with no loss
#: of information. A legend is added to the compact envelope so the meaning is
#: unambiguous to the model.
KEY_ALIASES: dict[str, str] = {
    "symbol": "sym",
    "qualname": "qn",
    "kind": "k",
    "file": "f",
    "line": "ln",
    "end_line": "eln",
    "signature": "sig",
    "doc": "doc",
    "name": "n",
    "anchor": "a",
    "depth": "d",
    "first_line_code": "code",
    "site_line": "sl",
    "resolved": "rv",
    "direction": "dir",
    "module": "m",
    "chain": "ch",
    "fan_in": "fi",
    "fan_out": "fo",
    "node": "nd",
    "edge_type": "et",
    "relation": "rel",
}

#: Free-text fields truncated (by characters) in compact mode -- these carry
#: prose/code whose tail rarely changes the agent's decision but inflates the
#: envelope. Keyed by the ALIASED name (post-rename).
_TRUNC_CHARS: dict[str, int] = {"sig": 120, "doc": 160, "code": 120}


def compact_item(item: Any) -> Any:
    """Shrink one result item: drop empties, alias keys, truncate free text.

    Non-dict items pass through unchanged. ``None``/``""``/``[]``/``{}`` values
    are dropped (the agent infers absence from the missing key). Keys are
    renamed via :data:`KEY_ALIASES`; long free-text fields are character-capped
    per :data:`_TRUNC_CHARS`. The transformation is lossy ONLY in the tails of
    signature/doc/code strings, never in anchors (file/line/name).
    """
    if not isinstance(item, dict):
        return item
    out: dict[str, Any] = {}
    for key, value in item.items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        alias = KEY_ALIASES.get(key, key)
        if isinstance(value, str):
            cap = _TRUNC_CHARS.get(alias)
            if cap is not None and len(value) > cap:
                value = value[:cap] + "…"
        out[alias] = value
    return out


def compact_serialize(envelope: dict) -> str:
    """Serialize a compact envelope to a no-whitespace JSON string.

    Returned as a STRING so FastMCP emits it verbatim (its ``_convert_to_content``
    pretty-prints dicts with ``indent=2`` but passes strings through). The
    items must already be compacted via :func:`compact_item`.
    """
    return json.dumps(envelope, separators=(",", ":"), ensure_ascii=False, default=str)

_ENCODER: Any = None
_ENCODER_FAILED: bool = False


def _get_encoder() -> Any:
    """Return the cached tiktoken o200k_base encoder, or None if unavailable."""
    global _ENCODER, _ENCODER_FAILED
    if _ENCODER is None and not _ENCODER_FAILED:
        try:
            import tiktoken

            _ENCODER = tiktoken.get_encoding("o200k_base")
        except Exception:
            _ENCODER_FAILED = True
    return _ENCODER


def serialize(obj: Any) -> str:
    """Serialize obj the way the wire format would (strings pass through)."""
    if isinstance(obj, str):
        return obj
    return json.dumps(obj, ensure_ascii=False, default=str)


def estimate_tokens(obj: Any) -> int:
    """Estimate the token count of obj's JSON serialization.

    Uses tiktoken o200k_base when loadable; otherwise falls back to
    ``len(serialized) // 4``.
    """
    text = serialize(obj)
    encoder = _get_encoder()
    if encoder is not None:
        try:
            return len(encoder.encode(text, disallowed_special=()))
        except Exception:
            pass
    return len(text) // 4


def fit(items: list, budget: int = TOKEN_BUDGET) -> tuple[list, bool]:
    """Drop tail items until the serialized item list fits within budget.

    Returns ``(kept_items, truncated)`` where kept_items is the longest
    prefix of items whose serialization fits in ``budget`` tokens, and
    truncated is True iff any items were dropped. Items are never reordered
    or mutated.
    """
    items = list(items)
    if estimate_tokens(items) <= budget:
        return items, False
    # Binary search for the longest prefix that fits.
    lo, hi = 0, len(items)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(items[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return items[:lo], True
