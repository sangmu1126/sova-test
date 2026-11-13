"""In-memory change feed double for unit tests (INV-08)."""

from sova.application.change_monitor import ChangeImpactSource
from sova.domain.change_impact import FeedWatermark


class MemoryChangeFeed:
    """Recorded events; every feed is complete up to ``clock()`` unless marked as a gap."""

    def __init__(self, clock):
        self.clock, self.recorded, self.gaps, self.down = clock, [], set(), False

    def events(self, *, application_id, environments, since):
        if self.down:
            raise ConnectionError("feed unavailable")
        return tuple(e for e in self.recorded if e.observed_at >= since)

    def watermarks(self, environments):
        if self.down:
            raise ConnectionError("feed unavailable")
        return tuple(FeedWatermark(e, self.clock()) for e in environments
                     if e not in self.gaps)


def memory_changes(clock):
    feed = MemoryChangeFeed(clock)
    return feed, ChangeImpactSource(feed=feed, clock=clock)
