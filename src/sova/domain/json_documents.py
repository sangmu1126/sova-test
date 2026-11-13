"""Bounded JSON decoding shared by domain documents and input adapters."""

import json

from sova.domain.contracts import ContractError, canonical_json


def json_document(raw, limit=262144):
    if not isinstance(raw, str):
        raise ContractError("Registration text required")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ContractError("Duplicate JSON key")
            result[key] = value
        return result

    try:
        if len(raw.encode()) > limit:
            raise ContractError("Registration size limit exceeded")
        data = json.loads(raw, object_pairs_hook=unique)
        canonical_json(data, limit=limit)
        return data
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise ContractError("Invalid registration JSON") from exc
