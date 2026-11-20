"""Qualification-only Harbor Docker extension; upstream execution stays intact.

Uses Harbor's documented custom environment import path. No installed wheel
file is modified. Only the auxiliary image selected by the operator is replaced.
This is not VM isolation, a production provider approval or an AI agent result.
"""

import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess

from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.task.config import NetworkMode
from harbor_helper_binding import qualified_helper


HELPER = "sha256:a53697aac3b133f389ed93fb43ddf2f1d173c74422a01a965f1e428c395b62e2"


def inspected():
    import harbor.environments.docker.docker as upstream

    if (
        version("harbor") != "0.26.0"
        or hashlib.sha256(Path(upstream.__file__).read_bytes()).hexdigest()
        != "55c6f0dfd41fdb07dc8e84d4d28eaf2aa774a07e7896b6568f1da849012276ba"
    ):
        raise ValueError(
            "Pinned unmodified upstream Harbor Docker implementation required"
        )
    path = os.environ.get("SOVA_HARBOR_HELPER_BINDING")
    if not path:
        raise ValueError("Operator helper inspection binding required")

    def inspect(image):
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=15,
        )
        values = json.loads(result.stdout)
        if len(values) != 1:
            raise ValueError("Single local image required")
        return values[0]

    actual = qualified_helper(path, inspect)
    if actual != HELPER:
        raise ValueError("This qualification accepts only the measured helper build")
    return actual


class InspectedDockerEnvironment(DockerEnvironment):
    # The upstream kernel probe must not introduce a second uninspected image.
    _EGRESS_CONTROL_KERNEL_PROBE_IMAGE = HELPER

    def __init__(self, *args, **kwargs):
        policy = kwargs.get("network_policy")
        phases = kwargs.get("phase_network_policies") or ()
        if policy is None or any(
            p.network_mode != NetworkMode.NO_NETWORK for p in (policy, *phases)
        ):
            raise ValueError("Explicit no-network startup and phases required")
        inspected()  # Before upstream's first kernel-probe container.
        super().__init__(*args, **kwargs)

    async def _ensure_egress_control_sidecar_image_built(self):
        import asyncio

        self._env_vars.egress_control_sidecar_image_name = await asyncio.to_thread(
            inspected
        )

    async def _apply_network_policy(self, network_policy):
        if network_policy.network_mode != NetworkMode.NO_NETWORK:
            raise ValueError(
                "Only the qualified public-source no-network exercise is allowed"
            )
        return await super()._apply_network_policy(network_policy)
