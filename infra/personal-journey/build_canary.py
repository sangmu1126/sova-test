"""Build a secret-free, digest-checked OCI canary using standard Docker Buildx.

Only explicit source files and a hashed dependency lock enter its context.
Never sends the repository, environment files, Git metadata or AWS settings.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile


FILES = (
    "src/sova/__init__.py", "src/sova/qualification/__init__.py",
    "src/sova/qualification/order_app.py", "src/sova/qualification/order_runtime.py",
    "src/sova/qualification/requests.py", "src/sova/service/__init__.py",
    "src/sova/service/http_boundary.py",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New private output directory")
    parser.add_argument("--kind", choices=("app", "postgres", "proxy"), default="app")
    args = parser.parse_args()
    os.umask(0o077)
    out = args.output.resolve()
    out.mkdir(parents=True, mode=0o700, exist_ok=False)
    root = Path(__file__).resolve().parents[2]
    context = out / "context"
    context.mkdir(mode=0o700)
    measured = {}
    sources = ((*FILES, "examples/order-canary/Dockerfile", "examples/order-canary/requirements.lock")
               if args.kind == "app" else ("examples/order-canary/" + (
                   "Postgres.Dockerfile" if args.kind == "postgres" else "Haproxy.Dockerfile"),))
    for source in sources:
        path = root / source
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 262144:
            raise ValueError("Bounded regular source file required")
        relative = source.removeprefix("src/") if source.startswith("src/") else Path(source).name
        if source.endswith(("Postgres.Dockerfile", "Haproxy.Dockerfile")):
            relative = "Dockerfile"
        destination = context / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = path.read_bytes()
        destination.write_bytes(data)
        measured[source] = hashlib.sha256(data).hexdigest()
    archive = out / "canary.oci.tar"
    with (out / "build.log").open("x") as log:
        result = subprocess.run(["docker", "buildx", "build", "--platform", "linux/arm64",
            "--provenance=false", "--sbom=false", "--output", "type=oci,dest=" + str(archive),
            str(context)], stdout=log, stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise RuntimeError("Pinned OCI build failed; inspect its private build.log")
    # Read only; never extract an archive or trust a name from its manifest.
    with tarfile.open(archive) as tar:
        index = json.load(tar.extractfile("index.json"))
        if len(index["manifests"]) != 1:
            raise ValueError("Exactly one native image manifest is required")
        manifest = index["manifests"][0]["digest"]
        if len(manifest) != 71 or not manifest.startswith("sha256:"):
            raise ValueError("SHA256 OCI manifest required")
        blob = tar.extractfile("blobs/sha256/" + manifest[7:]).read()
        if hashlib.sha256(blob).hexdigest() != manifest[7:]:
            raise ValueError("OCI manifest digest mismatch")
        image = json.loads(blob)
        digests = [image["config"]["digest"], *(layer["digest"] for layer in image["layers"])]
        for digest in digests:
            if len(digest) != 71 or not digest.startswith("sha256:"):
                raise ValueError("SHA256 OCI content required")
            content = tar.extractfile("blobs/sha256/" + digest[7:])
            hasher = hashlib.sha256()
            while chunk := content.read(1024 * 1024):
                hasher.update(chunk)
            if hasher.hexdigest() != digest[7:]:
                raise ValueError("OCI content digest mismatch")
        config = json.load(tar.extractfile("blobs/sha256/" + image["config"]["digest"][7:]))
        expected_user = "70:70" if args.kind == "postgres" else "10001:10001"
        if config["architecture"] != "arm64" or config["os"] != "linux" or config["config"]["User"] != expected_user:
            raise ValueError("The image is not the non-root native Linux canary")
    with archive.open("rb") as file:
        archive_digest = hashlib.file_digest(file, "sha256").hexdigest()
    report = {"kind": args.kind, "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "sources_sha256": measured, "manifest_digest": manifest,
        "config_digest": image["config"]["digest"], "architecture": "arm64", "os": "linux",
        "archive_sha256": archive_digest,
        "layers_verified": len(image["layers"]), "user": expected_user, "deployed": False}
    (out / "image.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("manifest_digest", "config_digest", "layers_verified", "deployed")}))


if __name__ == "__main__":
    main()
