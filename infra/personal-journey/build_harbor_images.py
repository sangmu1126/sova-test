"""Build only the two prepared public Harbor contexts with standard Buildx.

No repository-wide context, credentials, business data, agent or deployment.
The actual immutable image, every blob and the declared non-root architecture
are measured. A build still needs a separate fresh vulnerability inspection.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prepared", type=Path, required=True)
    p.add_argument("--kind", choices=("agent", "verifier"), required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    os.umask(0o077)
    prepared = json.loads((a.prepared / "preparation.json").read_text())
    context = a.prepared / (a.kind + "-context")
    expected = prepared["contexts"][a.kind]
    if prepared["classification"] != "public-source-only" or set(expected) != {
        x.name for x in context.iterdir()
    }:
        raise ValueError("Only the measured public exercise context is accepted")
    for name, digest in expected.items():
        path = context / name
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > 262144
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest
        ):
            raise ValueError("Prepared public build input changed")
    a.output.mkdir(mode=0o700, exist_ok=False)
    archive = a.output / "canary.oci.tar"
    with (a.output / "build.log").open("x") as log:
        subprocess.run(
            [
                "docker",
                "buildx",
                "build",
                "--platform",
                "linux/arm64",
                "--provenance=false",
                "--sbom=false",
                "--output",
                "type=oci,dest=" + str(archive),
                str(context),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=300,
        )
    with tarfile.open(archive) as tar:
        index = json.load(tar.extractfile("index.json"))
        if len(index["manifests"]) != 1:
            raise ValueError("Exactly one native manifest is required")

        def blob(digest):
            if len(digest) != 71 or not digest.startswith("sha256:"):
                raise ValueError("Content digest required")
            stream = tar.extractfile("blobs/sha256/" + digest[7:])
            h = hashlib.sha256()
            while chunk := stream.read(1024 * 1024):
                h.update(chunk)
            if h.hexdigest() != digest[7:]:
                raise ValueError("Built OCI content differs")

        manifest = index["manifests"][0]["digest"]
        blob(manifest)
        document = json.load(tar.extractfile("blobs/sha256/" + manifest[7:]))
        for item in (document["config"], *document["layers"]):
            blob(item["digest"])
        config = json.load(
            tar.extractfile("blobs/sha256/" + document["config"]["digest"][7:])
        )
        if (config["os"], config["architecture"], config["config"]["User"]) != (
            "linux",
            "arm64",
            "10001:10001",
        ):
            raise ValueError("Native non-root exercise image required")
    with archive.open("rb") as f:
        archive_sha = hashlib.file_digest(f, "sha256").hexdigest()
    result = {
        "kind": "harbor-" + a.kind,
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "sources_sha256": expected,
        "manifest_digest": manifest,
        "config_digest": document["config"]["digest"],
        "archive_sha256": archive_sha,
        "layers_verified": len(document["layers"]),
        "os": "linux",
        "architecture": "arm64",
        "user": "10001:10001",
        "deployed": False,
        "model_called": False,
        "preparation_sha256": hashlib.sha256(
            (a.prepared / "preparation.json").read_bytes()
        ).hexdigest(),
    }
    (a.output / "image.json").write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {"kind": result["kind"], "manifest": manifest, "model_called": False}
        )
    )


if __name__ == "__main__":
    main()
