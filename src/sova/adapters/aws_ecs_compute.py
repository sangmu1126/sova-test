"""Source compute on AWS ECS: stop it and read back that it stopped (§20.1).

A handoff is not independent of its source while the source application can
still run: its tasks may hold connections, drain queues or write elsewhere.
``EcsServiceCompute`` stops one registered ECS service and reads its state
back from ECS itself:

* ``stop_source_compute`` sets the service's desired count to zero
  (idempotent) and waits, bounded, until no task of the service is left in any
  state but STOPPED;
* the readback holds only if the service wants zero tasks, ECS counts none
  running or pending, and no task of the service is still on its way down.

``endpoints()`` gives the public addresses of the running tasks, so a probe
can later try the old address and confirm nothing answers there. Deleting the
service, its task definitions and images is retirement, a separate approved
step.
"""

import re
import time

from sova.domain.contracts import ContractError, EffectObservation, EffectStatus, canonical_json

ACTIONS = frozenset({"stop_source_compute"})
_NAME = re.compile(r"[A-Za-z0-9_-]{1,255}")


class EcsServiceCompute:
    def __init__(self, adapter_id, *, ecs, ec2, cluster, service, stop_wait_seconds=180.0):
        if not callable(getattr(ecs, "describe_services", None)) or not callable(
                getattr(ec2, "describe_network_interfaces", None)):
            raise ContractError("ECS and EC2 clients required")
        for value in (cluster, service):
            if type(value) is not str or not _NAME.fullmatch(value):
                raise ContractError("Registered ECS cluster and service names required")
        self.adapter_id, self.ecs, self.ec2 = adapter_id, ecs, ec2
        self.cluster, self.service = cluster, service
        self.stop_wait_seconds = stop_wait_seconds

    # -- reading ECS -----------------------------------------------------------
    def _service(self):
        found = self.ecs.describe_services(cluster=self.cluster, services=[self.service])
        services = [s for s in found.get("services", ()) if s.get("serviceName") == self.service]
        if len(services) != 1:
            raise ContractError("Registered ECS service not found")
        return services[0]

    def _tasks(self):
        """Every task of the service ECS still knows, with its last status."""
        arns = []
        for desired in ("RUNNING", "STOPPED"):
            pages = self.ecs.get_paginator("list_tasks").paginate(
                cluster=self.cluster, serviceName=self.service, desiredStatus=desired)
            arns += [arn for page in pages for arn in page.get("taskArns", ())]
        tasks = []
        for i in range(0, len(arns), 100):
            tasks += self.ecs.describe_tasks(cluster=self.cluster,
                                             tasks=arns[i:i + 100]).get("tasks", ())
        return tasks

    def observe(self):
        service = self._service()
        alive = sorted(t["taskArn"].rsplit("/", 1)[-1] for t in self._tasks()
                       if t.get("lastStatus") != "STOPPED")
        stopped = (service.get("desiredCount") == 0 and service.get("runningCount") == 0
                   and service.get("pendingCount") == 0 and not alive)
        return stopped, {"service": self.service, "status": service.get("status"),
                         "desired": service.get("desiredCount"),
                         "running": service.get("runningCount"),
                         "pending": service.get("pendingCount"), "tasks_not_stopped": alive[:20]}

    def endpoints(self):
        """Public IPv4 addresses of the service's running tasks."""
        interfaces = [d["value"] for t in self._tasks() if t.get("lastStatus") == "RUNNING"
                      for a in t.get("attachments", ()) for d in a.get("details", ())
                      if d.get("name") == "networkInterfaceId"]
        if not interfaces:
            return []
        found = self.ec2.describe_network_interfaces(NetworkInterfaceIds=interfaces)
        return sorted(i["Association"]["PublicIp"] for i in found["NetworkInterfaces"]
                      if i.get("Association", {}).get("PublicIp"))

    # -- EffectAdapter -------------------------------------------------------
    def validate(self, command):
        if command.adapter_id != self.adapter_id or command.action not in ACTIONS:
            raise ContractError("Unsupported compute action for this adapter")

    def apply(self, command):
        if self._service().get("desiredCount") != 0:
            self.ecs.update_service(cluster=self.cluster, service=self.service, desiredCount=0)
        deadline = time.monotonic() + self.stop_wait_seconds
        while not self.observe()[0] and time.monotonic() < deadline:
            time.sleep(5)

    def readback(self, command, now):
        stopped, detail = self.observe()
        return EffectObservation(
            command.scope, command.fingerprint,
            EffectStatus.APPLIED if stopped else EffectStatus.UNKNOWN, now,
            canonical_json({"action": command.action, **detail}, limit=65536))
