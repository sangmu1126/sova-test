"""Measured native-process proof producer for qualification only, never a Pod proof.

application_name is a trusted test tag, not a production authenticated identity.
Every remote connection and relay is created and controlled by this fixture.
"""

from datetime import timedelta
import hashlib
import json

import psycopg

from sova.domain.contracts import ContractError
from sova.domain.execution import QuiescenceProof, pending_digest


def remote_sessions(h, owner):
    with psycopg.connect(h.business['dsn']) as conn:
        return [{'pid': r[0], 'state': r[1], 'wait_event': r[2]} for r in conn.execute(
            'SELECT pid,state,wait_event FROM pg_stat_activity WHERE application_name=%s '
            'AND datname=ANY(%s) ORDER BY pid', (owner, list(h.business['names'].values())))]


class NativeSettlement:
    def __init__(self, h, lost, relay):
        self.h, self.lost, self.relay = h, lost, relay
        self.issued, self.observations = {}, []

    def prove(self, lifecycle, tenant, app, operation, pending):
        if (tenant, app) != (self.h.tenant, self.h.app):
            return None
        with lifecycle.transaction() as conn:
            row = conn.execute('SELECT owner_id,epoch,valid_until,clock_timestamp() FROM sova_control.app_execution '
                'WHERE tenant_id=%s AND application_id=%s AND operation_id=%s', (tenant, app, operation)).fetchone()
        if row is None or row[0]!=self.lost.owner:
            return None
        try:
            sessions = remote_sessions(self.h, self.lost.owner)
        except psycopg.Error:
            self.observations.append({'remote_state': 'unknown'})
            return None
        observation = {'owner': self.lost.owner, 'process_exit': self.lost.process.poll(),
            'relay_stopped': self.relay.stopped.is_set() and not any(t.is_alive() for t in self.relay.threads),
            'remote_sessions': sessions, 'lease_expired': row[2]<=row[3], 'epoch': row[1],
            'pending_digest': pending_digest(pending), 'observed_at': row[3].isoformat()}
        self.observations.append(observation)
        if observation['process_exit'] is None or not observation['relay_stopped'] or sessions or not observation['lease_expired']:
            return None
        digest = hashlib.sha256(json.dumps(observation, sort_keys=True).encode()).hexdigest()
        proof = QuiescenceProof(tenant, app, operation, row[1], pending_digest(pending),
                                digest, row[3], row[3]+timedelta(seconds=30))
        self.issued[digest] = proof
        return proof

    def authenticate(self, proof):
        if self.issued.get(proof.artifact_digest)!=proof or self.h.clock()>=proof.valid_until:
            raise ContractError('Native test settlement proof is unissued or expired')
