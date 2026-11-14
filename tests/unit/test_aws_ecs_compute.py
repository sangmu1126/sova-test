"""ECS source-compute decisions against a stub (no AWS)."""

from datetime import UTC, datetime, timedelta

import pytest

from sova.adapters.aws_ecs_compute import EcsServiceCompute
from sova.domain.contracts import ContractError, EffectCommand, EffectStatus, ResourceScope


class Ecs:
    def __init__(self, service, tasks):
        self.service, self.tasks, self.updates = service, tasks, []

    def describe_services(self, *, cluster, services):
        return {"services": [{"serviceName": services[0], **self.service}]}

    def get_paginator(self, operation):
        stub = self

        class Pages:
            def paginate(self, *, cluster, serviceName, desiredStatus):
                yield {"taskArns": [t["taskArn"] for t in stub.tasks
                                    if t["desired"] == desiredStatus]}
        return Pages()

    def describe_tasks(self, *, cluster, tasks):
        return {"tasks": [t for t in self.tasks if t["taskArn"] in tasks]}

    def update_service(self, **kwargs):
        self.updates.append(kwargs)
        self.service["desiredCount"] = 0


class Ec2:
    def describe_network_interfaces(self, *, NetworkInterfaceIds):
        return {"NetworkInterfaces": [{"Association": {"PublicIp": "198.51.100.7"}}
                                      for _ in NetworkInterfaceIds]}


def task(arn, last, desired="RUNNING"):
    return {"taskArn": f"arn:aws:ecs:r:a:task/c/{arn}", "lastStatus": last,
            "desired": desired, "attachments": [{"details": [
                {"name": "networkInterfaceId", "value": "eni-1"}]}]}


def compute(ecs):
    return EcsServiceCompute("ecs", ecs=ecs, ec2=Ec2(), cluster="c", service="orders",
                             stop_wait_seconds=0)


def command():
    return EffectCommand(ResourceScope("t", "a", "e", "r"), "op", "stop", "ecs",
                         "stop_source_compute", "op:stop", "b" * 64, "r", 1,
                         datetime.now(UTC) + timedelta(minutes=1), "{}")


def test_desired_zero_is_not_stopped_while_a_task_is_still_going_down():
    ecs = Ecs({"desiredCount": 0, "runningCount": 0, "pendingCount": 0},
              [task("t1", "DEACTIVATING", desired="STOPPED")])
    stopped, detail = compute(ecs).observe()
    assert not stopped and detail["tasks_not_stopped"] == ["t1"]
    ecs.tasks[0]["lastStatus"] = "STOPPED"
    assert compute(ecs).observe()[0]


def test_stopping_is_idempotent_and_the_readback_reads_ecs():
    ecs = Ecs({"desiredCount": 1, "runningCount": 1, "pendingCount": 0},
              [task("t1", "RUNNING")])
    adapter = compute(ecs)
    assert adapter.endpoints() == ["198.51.100.7"]
    adapter.apply(command())
    assert ecs.updates == [{"cluster": "c", "service": "orders", "desiredCount": 0}]
    assert adapter.readback(command(), datetime.now(UTC)).status == EffectStatus.UNKNOWN
    ecs.service.update(runningCount=0)
    ecs.tasks[0].update(lastStatus="STOPPED", desired="STOPPED")
    adapter.apply(command())
    assert len(ecs.updates) == 1  # already zero: no second change
    assert adapter.readback(command(), datetime.now(UTC)).status == EffectStatus.APPLIED


def test_unregistered_names_are_refused():
    with pytest.raises(ContractError):
        EcsServiceCompute("ecs", ecs=Ecs({}, []), ec2=Ec2(), cluster="c", service="bad name")
