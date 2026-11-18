"""The volume unit with its agents reached through the API server's
port-forward instead of its Pod proxy: the conformance kit holds the same,
and the transport probe tells the hops - passing only where the operator's
inventory says the API server verifies its kubelets."""

from adapter_conformance import HandoffAdapterConformance
import pytest
from volume_world import VolumeWorld

from sova.adapters.kubernetes.volume_agents import VolumeAgents
from sova.adapters.kubernetes_volume_probes import KubernetesVolumeProbes

pytestmark = pytest.mark.kubernetes


class PortForwardWorld(VolumeWorld):
    transport = "port-forward"


@pytest.fixture
def world(volume_business):
    return PortForwardWorld(volume_business)


class TestPortForwardConformance(HandoffAdapterConformance):
    pass


def test_the_transport_probe_tells_the_hops(world):
    adapter = world.adapter()

    def probes(kubelet, transport="port-forward"):
        agents = VolumeAgents(label="probe" + adapter.unit[:10], key=world.KEY,
                              image=adapter.agents.image, run_as=adapter.agents.run_as,
                              fs_group=adapter.agents.fs_group,
                              transport=transport)
        return KubernetesVolumeProbes(source=adapter.sides["source"],
                                      target=adapter.sides["target"], agents=agents,
                                      kubelet_tls_verified=kubelet)

    passed, observation = probes(lambda side: True).transport_encrypted("target")
    assert passed and observation["transport"] == "port-forward"
    assert observation["hops"]["kubelet_to_pod"] == "within the node"
    passed, observation = probes(None).transport_encrypted("target")
    assert not passed and observation["client_verification"] == "partial"
    passed, observation = probes(lambda side: True, "proxy").transport_encrypted("target")
    assert not passed and observation["transport"] == "proxy"  # the clear-text hop remains
