"""Plain-data JSON codec for persistent caches (自举友好).

Pickle stores a Python object graph — class references and memoized
instances only this interpreter understands.  A future self-hosted
(CWind-written) frontend could never read it, and a class refactor
silently invalidates every entry.  The on-disk caches therefore store
**plain JSON with a small explicit tagging scheme**:

* dataclass -> ``{"$c": "<ClassName>", "a": [<field values>...]}``
  (positional constructor arguments in class-field order)
* enum      -> ``{"$e": "<EnumName>", "v": <encoded value>}``
* tuple     -> ``{"$r": [<items>...]}``
* ``pathlib.Path`` -> ``{"$p": "<path>"}`
* str / int / float / bool / None / list / str-keyed dict -> pass through

The class REGISTRY is supplied by each cache owner — no import cycles,
and a name missing from the registry raises :class:`ValueError` so the
loader can treat the file as a cold cache.  The envelope (version tag +
entries) stays the owner's business; wire encoding is compact UTF-8
JSON.  Because the tagged document is itself ordinary data, a future
speed problem can swap the wire layer (msgpack/bson) without touching
the schema.
"""

from __future__ import annotations

import json
from dataclasses import fields as _dc_fields
from dataclasses import is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

__all__ = ["encode", "decode", "dumps", "loads"]


def encode(value: Any, registry: Mapping[str, type]) -> Any:
    """Python cache object -> JSON-native tagged document."""
    if isinstance(value, Enum):
        return {"$e": type(value).__name__, "v": encode(value.value, registry)}
    if isinstance(value, Path):
        return {"$p": str(value)}
    if is_dataclass(value) and not isinstance(value, type):
        name = type(value).__name__
        if name not in registry:
            raise ValueError(f"type '{name}' is not cache-encodable")
        return {
            "$c": name,
            "a": [encode(getattr(value, f.name), registry)
                  for f in _dc_fields(value)],
        }
    if isinstance(value, tuple):
        return {"$r": [encode(v, registry) for v in value]}
    if isinstance(value, list):
        return [encode(v, registry) for v in value]
    if isinstance(value, dict):
        return {k: encode(v, registry) for k, v in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ValueError(f"value of type '{type(value).__name__}' is not "
                     "cache-encodable")


def decode(document: Any, registry: Mapping[str, type]) -> Any:
    """Inverse of :func:`encode` (ValueError = treat cache as cold)."""
    if isinstance(document, dict):
        tag = document.get("$c")
        if tag is not None:
            cls = registry.get(tag)
            if cls is None:
                raise ValueError(f"unknown cached type '{tag}'")
            return cls(*[decode(v, registry) for v in document["a"]])
        tag = document.get("$e")
        if tag is not None:
            cls = registry.get(tag)
            if cls is None:
                raise ValueError(f"unknown cached enum '{tag}'")
            return cls(decode(document["v"], registry))
        if "$p" in document:
            return Path(document["$p"])
        if "$r" in document:
            return tuple(decode(v, registry) for v in document["$r"])
        return {k: decode(v, registry) for k, v in document.items()}
    if isinstance(document, list):
        return [decode(v, registry) for v in document]
    return document


def dumps(document: Any, registry: Mapping[str, type]) -> bytes:
    """Encode + serialize one cache document (compact UTF-8 JSON)."""
    return json.dumps(
        encode(document, registry),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=True,
    ).encode("utf-8")


def loads(blob: bytes, registry: Mapping[str, type]) -> Any:
    """Deserialize + decode one cache document."""
    return decode(json.loads(blob), registry)
