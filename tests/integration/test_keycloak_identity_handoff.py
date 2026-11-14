"""An account set handed off as its own unit (ADR-0002): the conformance kit
every data adapter passes, against two real Keycloak servers.

Writers are real paths: the realm's self-registration form (the application)
and a back-office client whose service account may manage users (another
registered writer). Every write attempt creates a real account and removes it.
"""

from uuid import uuid4

from adapter_conformance import HandoffAdapterConformance, apply, readback
from identity_world import BACKOFFICE, IdentityWorld
import pytest

from sova.adapters.keycloak_identity_handoff import PREFENCE
from sova.domain.contracts import EffectStatus

pytestmark = pytest.mark.keycloak
@pytest.fixture
def identity_world(keycloak_servers):
    realm = "accounts-" + uuid4().hex[:10]
    world = IdentityWorld(keycloak_servers, realm)
    yield world
    for side in ("source", "target"):
        keycloak_servers[side].call("DELETE", f"/{realm}")


class TestKeycloakIdentityHandoffConformance(HandoffAdapterConformance):
    @pytest.fixture
    def world(self, identity_world):
        return identity_world


def test_an_abort_restores_every_source_writer(identity_world):
    """Fenced, then aborted before activation: the settings in force before the
    fence come back, and both writer paths really work again."""
    world = identity_world
    for action in ("stage_target", "start_sync", "pause_admission", "fence_source_writes"):
        apply(world.adapter(), action)
    assert not world.can_write("source", "app") and not world.can_write("source", "other")
    world.adapter().restore_source()
    restored, detail = world.adapter().source_restored()
    assert restored, detail
    assert world.can_write("source", "app") and world.can_write("source", "other")
    world.adapter().restore_source()  # idempotent
    attributes = world._call("source", "GET", "").get("attributes") or {}
    assert not attributes.get(PREFENCE)


def test_brokered_sign_ups_keep_the_fence_unconfirmed(identity_world):
    """An enabled identity provider can create accounts at first login even
    with registration off: the fence is not confirmed while one is enabled."""
    world = identity_world
    world._call("source", "POST", "/identity-provider/instances", {
        "alias": "corporate", "providerId": "oidc", "enabled": True,
        "config": {"clientId": "sova-test", "clientSecret": "unused",
                   "authorizationUrl": "http://127.0.0.1:9/auth",
                   "tokenUrl": "http://127.0.0.1:9/token"}})
    for action in ("pause_admission", "fence_source_writes"):
        apply(world.adapter(), action)
    assert readback(world.adapter(), "fence_source_writes") == EffectStatus.UNKNOWN
    holds, detail = world.adapter()._check_fence_source_writes()
    assert not holds and detail["identity_providers"] == ["corporate"]


def test_the_probes_decide_independently(identity_world):
    """The independent probes agree with the adapter's readback at each point."""
    world = identity_world
    source = world.probes("source")
    assert source.denied()[0] is False  # both writers still work
    for action in ("pause_admission", "fence_source_writes"):
        apply(world.adapter(), action)
    denied, observation = source.denied()
    assert denied, observation
    assert set(observation["paths"]) == {"self_registration", BACKOFFICE}
