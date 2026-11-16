"""Readback evidence is shortened, never refused: an observation accepts at
most 8 KiB, and a readback that saw a lot must still deliver its verdict."""

from datetime import UTC, datetime
import json

from sova.adapters.evidence_bounds import bounded
from sova.domain.contracts import (
    EffectObservation,
    EffectStatus,
    ResourceScope,
    canonical_json,
)


def observation(detail):
    return EffectObservation(ResourceScope("t", "a", "e", "r"), "c" * 64, EffectStatus.UNKNOWN,
                             datetime.now(UTC),
                             canonical_json({"action": "verify_final_state", **bounded(detail)},
                                            limit=8192))


def test_small_evidence_is_kept_as_it_is():
    detail = {"route": "board_tgt", "claims_bound": {"source": True}}
    assert bounded(detail) == detail


def test_a_long_list_is_cut_and_the_cut_is_said():
    differences = [{"path": f"uploads/{i:05}.txt", "source": ["f", 420, 10001, i, "a" * 64],
                    "target": None} for i in range(400)]
    detail = {"claims": [{"source_claim": "board-data", "differences": differences,
                          "source": {"files": 400}, "target": {"files": 0}}]}
    seen = observation(detail).evidence
    assert len(json.dumps(seen).encode()) <= 8192
    kept = seen["claims"][0]
    assert kept["source"] == {"files": 400} and kept["source_claim"] == "board-data"
    assert 0 < len(kept["differences"]) < 400
    assert kept["differences"][0]["path"] == "uploads/00000.txt"  # the first ones stay
    left_out = seen["evidence_shortened"]["items_left_out"]["claims/0/differences"]
    assert left_out == 400 - len(kept["differences"])


def test_large_objects_without_lists_give_way_to_their_digest():
    detail = {"verdict": "differs", "tables": {f"t{i}": "x" * 100 for i in range(200)}}
    seen = observation(detail).evidence
    assert seen["verdict"] == "differs" and set(seen["tables"]) == {"sha256"}
