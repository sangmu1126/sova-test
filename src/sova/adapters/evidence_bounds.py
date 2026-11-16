"""Readback evidence that always fits the observation it goes into.

An ``EffectObservation`` accepts at most 8 KiB of evidence. An adapter whose
readback lists what it saw (differing files, open sessions, components) must
not fail the readback because the list was long: the verdict comes first, the
detail is shortened. Lists are cut from the end, longest first, and what was
cut is said; if that is not enough, nested objects give way to a digest of
their full content.
"""

import hashlib
import json

LIMIT = 8000  # under the 8192-byte bound, with room for the action name


def _size(value):
    return len(json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode())


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _lists(value, path=()):
    """Every list inside ``value``, with the path to it."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _lists(item, (*path, key))
    elif isinstance(value, list):
        yield path, value
        for i, item in enumerate(value):
            yield from _lists(item, (*path, i))


def _at(value, path):
    for step in path:
        value = value[step]
    return value


def bounded(detail, limit=LIMIT):
    """``detail`` itself if it fits in ``limit`` bytes, else a shortened copy that
    keeps every scalar it can and says what was left out."""
    if _size(detail) <= limit:
        return detail
    shortened = json.loads(json.dumps(detail))
    cut = {}
    while _size(shortened) > limit:
        found = [(len(items) > 1, len(json.dumps(items)), path, items)
                 for path, items in _lists(shortened) if items]
        if not found:
            break
        # A list of one entry is cut only when no longer list is left: its entry
        # may hold the long list that should give way instead.
        _, _, path, items = max(found, key=lambda f: (f[0], f[1]))
        keep = max(len(items) // 2, 0)
        key = "/".join(map(str, path))
        cut[key] = cut.get(key, 0) + len(items) - keep
        container = _at(shortened, path[:-1]) if path else None
        if container is None:
            break
        container[path[-1]] = items[:keep]
    if _size(shortened) > limit:  # objects too large without any list: keep their digests
        shortened = {k: (v if _size(v) <= 256 else {"sha256": _digest(v)})
                     for k, v in shortened.items()}
    shortened["evidence_shortened"] = {"full_sha256": _digest(detail),
                                       "items_left_out": dict(sorted(cut.items())[:20])}
    return shortened
