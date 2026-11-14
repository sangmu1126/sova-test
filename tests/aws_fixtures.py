"""Opt-in fixtures for tests against a real AWS account.

Nothing here runs unless ``SOVA_TEST_AWS_LIVE=1`` names the intent and
``SOVA_TEST_AWS_PROFILE``, ``SOVA_TEST_AWS_REGION`` and ``SOVA_TEST_AWS_RUN``
(a short run id) are set. Every resource these tests create carries
``sova-test-<run>`` in its name and a ``sova-test`` tag, so a run-scoped
cleanup removes exactly what was created and nothing else. Credentials come
from the named local profile and are never printed.
"""

import json
import os
import re
import time

import pytest

TRUST_WAIT_SECONDS = 90


@pytest.fixture(scope="session")
def aws():
    if os.environ.get("SOVA_TEST_AWS_LIVE") != "1":
        pytest.skip("SOVA_TEST_AWS_LIVE=1 is required for tests against a real AWS account")
    boto3 = pytest.importorskip("boto3")
    run = os.environ.get("SOVA_TEST_AWS_RUN", "")
    if not re.fullmatch(r"[a-z0-9]{4,8}", run):
        raise pytest.UsageError("SOVA_TEST_AWS_RUN must be a short lowercase run id")
    session = boto3.Session(profile_name=os.environ["SOVA_TEST_AWS_PROFILE"],
                            region_name=os.environ["SOVA_TEST_AWS_REGION"])
    caller = session.client("sts").get_caller_identity()["Arn"]
    return {"session": session, "run": run, "region": session.region_name, "caller": caller,
            "tags": [{"Key": "sova-test", "Value": run}]}


@pytest.fixture(scope="module")
def aws_writer_roles(aws):
    """Two IAM roles the caller may assume: the application and a batch job.
    They hold no permissions of their own; bucket policies decide."""
    iam = aws["session"].client("iam")
    trust = json.dumps({"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"AWS": aws["caller"]}, "Action": "sts:AssumeRole"}]})
    roles = {}
    for writer in ("app", "batch"):
        name = f"sova-test-{writer}-{aws['run']}"
        try:
            arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=trust,
                                  MaxSessionDuration=3600, Tags=aws["tags"],
                                  Description="sova test writer, deleted after the run")["Role"]["Arn"]
        except iam.exceptions.EntityAlreadyExistsException:
            arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        roles[writer] = arn
    sts = aws["session"].client("sts")
    clients = {}
    deadline = time.monotonic() + TRUST_WAIT_SECONDS
    for writer, arn in roles.items():
        while True:  # IAM is eventually consistent: wait until the role can be assumed
            try:
                credentials = sts.assume_role(RoleArn=arn, RoleSessionName="sova-test",
                                              DurationSeconds=3600)["Credentials"]
                break
            except sts.exceptions.ClientError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(3)
        clients[writer] = credentials
    time.sleep(10)  # bucket policies resolve new principals a little later still
    yield {"arn": roles, "credentials": clients}
    for writer in roles:
        iam.delete_role(RoleName=f"sova-test-{writer}-{aws['run']}")
