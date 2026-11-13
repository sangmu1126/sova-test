"""In-memory authority witness double; production keeps it outside the database."""

import copy


class MemoryAuthorityWitness:
    def __init__(self):
        self.entries = []

    def append(self, application_id, unit_digest, action, document):
        self.entries.append(((application_id, unit_digest), action, copy.deepcopy(document)))

    def latest(self, application_id, unit_digest):
        found = [d for key, _, d in self.entries if key == (application_id, unit_digest)]
        return copy.deepcopy(found[-1]) if found else None
