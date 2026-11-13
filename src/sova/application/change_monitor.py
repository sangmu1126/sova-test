"""Assess change impact for an admitted or executing handoff (INV-08).

``ChangeImpactSource`` reads the recorded change events and collector
watermarks that a plan depends on from a change feed and decides their impact
with ``assess_impact``. The feed port is::

    events(*, application_id, environments, since) -> tuple[ChangeEvent, ...]
    watermarks(environments) -> tuple[FeedWatermark, ...]

A feed that cannot answer is not "no change": the caller receives an
assessment that allows no commands and counts no evidence.
"""

from sova.domain.change_impact import (
    MAX_FEED_LAG,
    ImpactAssessment,
    ImpactIndex,
    assess_impact,
)
from sova.domain.contracts import ContractError, utc


class ChangeImpactSource:
    def __init__(self, *, feed, clock, max_feed_lag=MAX_FEED_LAG):
        if not callable(clock) or not all(
            callable(getattr(feed, name, None)) for name in ("events", "watermarks")
        ):
            raise ContractError("Change feed and trusted clock required")
        self.feed, self.clock, self.max_feed_lag = feed, clock, max_feed_lag

    def __call__(self, revision, plan, *, issued):
        index = ImpactIndex.from_plan(revision, plan)
        now = utc(self.clock())
        environments = tuple(sorted(dict(index.side_of_environment)))
        try:
            events = tuple(self.feed.events(application_id=index.application_id,
                                            environments=environments,
                                            since=index.snapshot_observed_at))
            marks = tuple(self.feed.watermarks(tuple(sorted(index.required_feeds))))
            return assess_impact(index, events, marks, issued=issued, now=now,
                                 max_feed_lag=self.max_feed_lag)
        except Exception:
            # Unreachable feed, oversized history, or an inconsistent record:
            # the impact is unknown, never "unchanged".
            return ImpactAssessment(index.plan_digest, now, False, True, (),
                                    frozenset(o[0] for o in index.obligations),
                                    ("CHANGE_FEED_UNAVAILABLE",))
