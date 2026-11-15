"""The content digest every data adapter and probe shares, independent of the store.

The row count, and SHA-256 over every row's text form (UTF-8), sorted
bytewise and each prefixed with its length as 8 big-endian bytes. Equal
content gives an equal digest whatever the physical row order or the
declared key's uniqueness. How a row becomes text is the store's business
(``postgres_digest``, ``mysql_offline_copy``); the framing is the same.
"""

import hashlib


def client_digest(texts):
    rows = sorted(text.encode() for text in texts)
    h = hashlib.sha256()
    for row in rows:
        h.update(len(row).to_bytes(8, "big") + row)
    return {"rows": len(rows), "sha256": h.hexdigest()}
