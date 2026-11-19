"""Publish inspected public-only exercise images to the owned immutable ECR.

Uses standard Docker/ECR. Short-lived registry auth is confined to a new
private config directory and removed afterwards; it never enters the guest,
Git, command arguments or task inputs. Registry manifest identity must match.
"""

import argparse
import base64
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import boto3
from botocore.config import Config

from prepare_canary import artifact


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--agent-image", type=Path, required=True)
    p.add_argument("--verifier-image", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    os.umask(0o077)
    a.output.mkdir(mode=0o700, exist_ok=False)
    session = boto3.Session(profile_name="default", region_name="ap-northeast-2")
    cfg = Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 1})
    assert (
        session.client("sts", config=cfg).get_caller_identity()["Account"]
        == "265233844540"
    )
    client = session.client("ecr", config=cfg)
    repo = client.describe_repositories(repositoryNames=["sova-journey-b0be270e62"])[
        "repositories"
    ][0]
    tags = {
        t["Key"]: t["Value"]
        for t in client.list_tags_for_resource(resourceArn=repo["repositoryArn"])[
            "tags"
        ]
    }
    if repo["imageTagMutability"] != "IMMUTABLE" or tags.get("RunId") != "b0be270e62":
        raise ValueError("Exact owned immutable exercise repository required")
    uri = repo["repositoryUri"]
    registry = uri.split("/")[0]
    auth_root = Path(tempfile.mkdtemp(prefix="sova-owned-ecr-", dir=a.output))
    env = {**os.environ, "DOCKER_CONFIG": str(auth_root)}
    report = {
        "repository": uri,
        "published_at": datetime.now(UTC).isoformat(),
        "images": {},
        "registry_credentials_removed": False,
        "model_called": False,
        "production_changed": False,
    }
    try:
        auth = client.get_authorization_token(registryIds=["265233844540"])[
            "authorizationData"
        ][0]
        if auth["proxyEndpoint"] != "https://" + registry:
            raise ValueError("Registry credential belongs to another endpoint")
        username, password = base64.b64decode(auth["authorizationToken"]).split(b":", 1)
        subprocess.run(
            [
                "docker",
                "login",
                "--username",
                username.decode(),
                "--password-stdin",
                registry,
            ],
            input=password,
            env=env,
            capture_output=True,
            check=True,
            timeout=30,
        )
        for kind, folder in (("agent", a.agent_image), ("verifier", a.verifier_image)):
            proof = artifact(folder)
            if proof["kind"] != "harbor-" + kind:
                raise ValueError("Wrong exercise image kind")
            tag = "harbor-" + kind + "-" + proof["manifest_digest"][7:23]
            subprocess.run(
                ["docker", "load", "--input", str(folder / "canary.oci.tar")],
                env=env,
                capture_output=True,
                check=True,
                timeout=90,
            )
            subprocess.run(
                ["docker", "tag", proof["manifest_digest"], uri + ":" + tag],
                env=env,
                capture_output=True,
                check=True,
                timeout=20,
            )
            with (a.output / (kind + "-push.log")).open("x") as log:
                subprocess.run(
                    ["docker", "push", uri + ":" + tag],
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=True,
                    timeout=300,
                )
            response = client.batch_get_image(
                repositoryName=repo["repositoryName"], imageIds=[{"imageTag": tag}]
            )
            if response.get("failures") or len(response.get("images", [])) != 1:
                raise ValueError("Registry image is not readable")
            image = response["images"][0]
            manifest = image["imageManifest"].encode()
            if (
                image["imageId"]["imageDigest"] != proof["manifest_digest"]
                or "sha256:" + hashlib.sha256(manifest).hexdigest()
                != proof["manifest_digest"]
            ):
                raise ValueError("Registry changed the measured OCI manifest")
            ref = uri + "@" + proof["manifest_digest"]
            with (a.output / (kind + "-pull.log")).open("x") as log:
                subprocess.run(
                    ["docker", "pull", "--platform", "linux/arm64", ref],
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=True,
                    timeout=180,
                )
            observed = json.loads(
                subprocess.check_output(
                    ["docker", "image", "inspect", ref], env=env, timeout=20
                )
            )[0]
            if (
                ref not in observed.get("RepoDigests", [])
                or observed["Architecture"] != "arm64"
                or observed["Config"]["User"] != "10001:10001"
            ):
                raise ValueError("Actual cached runtime reference differs")
            report["images"][kind] = {
                "reference": ref,
                "manifest_digest": proof["manifest_digest"],
                "config_digest": proof["config_digest"],
                "archive_sha256": proof["archive_sha256"],
                "registry_readback_verified": True,
                "actual_runtime_reference_verified": True,
            }
            (a.output / "publication.json").write_text(
                json.dumps(report, indent=2) + "\n"
            )
    finally:
        shutil.rmtree(auth_root)
        report["registry_credentials_removed"] = not auth_root.exists()
        (a.output / "publication.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "published_images": len(report["images"]),
                "registry_credentials_removed": report["registry_credentials_removed"],
                "model_called": False,
            }
        )
    )


if __name__ == "__main__":
    main()
