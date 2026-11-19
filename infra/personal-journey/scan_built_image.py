"""Use standard Docker/Trivy to inspect a measured native OCI build.

This does not suppress findings or deploy the image. A fresh vulnerability
database, archive hash and actual native/non-root loaded image are required.
The caller owns the supplied private artifact/cache directories.
"""

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile


def bind_config(image, scan, archive):
    """Docker's OCI backend may report manifest ID rather than config ID.

    Verify the actual OCI config blob and the scanner's uncompressed layer
    identities instead of pretending those two IDs must be identical.
    """
    with tarfile.open(archive) as tar:
        raw = tar.extractfile(
            "blobs/sha256/" + image["config_digest"].removeprefix("sha256:")
        ).read()
    if "sha256:" + hashlib.sha256(raw).hexdigest() != image["config_digest"]:
        raise ValueError("OCI config blob differs from the measured build")
    config = json.loads(raw)
    metadata = scan.get("Metadata", {})
    if (
        metadata.get("ImageID")
        not in {image["manifest_digest"], image["config_digest"]}
        or metadata.get("DiffIDs") != config.get("rootfs", {}).get("diff_ids")
        or not metadata.get("DiffIDs")
    ):
        raise ValueError("Scanner runtime identity/layers differ from the built config")
    return {
        "config_digest": image["config_digest"],
        "scanner_image_id": metadata["ImageID"],
        "diff_ids_verified": len(metadata["DiffIDs"]),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("image", "trivy", "cache"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument(
        "--derive-output",
        type=Path,
        help="New receipt directory for binding an existing raw scan; preserve every old byte",
    )
    a = p.parse_args()
    os.umask(0o077)
    metadata_path = a.cache / "db/metadata.json"
    metadata = json.loads(metadata_path.read_text())
    now = datetime.now(UTC)
    if (
        not datetime.fromisoformat(metadata["UpdatedAt"].replace("Z", "+00:00"))
        <= now
        < datetime.fromisoformat(metadata["NextUpdate"].replace("Z", "+00:00"))
    ):
        raise ValueError("Current vulnerability database required")
    image = json.loads((a.image / "image.json").read_text())
    if a.derive_output:
        scan = json.loads((a.image / "scan.json").read_text())
        with (a.image / "canary.oci.tar").open("rb") as f:
            if hashlib.file_digest(f, "sha256").hexdigest() != image["archive_sha256"]:
                raise ValueError("Existing built archive changed")
        binding = bind_config(image, scan, a.image / "canary.oci.tar")
        old = json.loads((a.image / "scan-summary.json").read_text())
        if (
            old["manifest_digest"] != image["manifest_digest"]
            or old["preflight"] != "PASS"
            or any(old["counts"].values())
        ):
            raise ValueError("Do not derive a passing receipt from a blocked image")
        if any(
            r.get("Vulnerabilities") for r in scan.get("Results", [])
        ) or not scan.get("Results"):
            raise ValueError("Actual raw scan must have results and zero findings")
        a.derive_output.mkdir(mode=0o700, exist_ok=False)
        for name in ("image.json", "canary.oci.tar", "scan.json"):
            os.link(a.image / name, a.derive_output / name)
        summary = {
            **old,
            "config_digest": binding["config_digest"],
            "config_binding": binding,
            "derived_from_summary_sha256": hashlib.sha256(
                (a.image / "scan-summary.json").read_bytes()
            ).hexdigest(),
            "derived_at": datetime.now(UTC).isoformat(),
            "new_scan_performed": False,
            "vulnerability_db_observed_at_derivation": metadata,
            "vulnerability_db_metadata_sha256_at_derivation": hashlib.sha256(
                metadata_path.read_bytes()
            ).hexdigest(),
        }
        (a.derive_output / "scan-summary.json").write_text(
            json.dumps(summary, indent=2) + "\n"
        )
        print(
            json.dumps(
                {
                    "derived_config_binding": binding,
                    "old_receipts_preserved": True,
                    "new_scan_performed": False,
                }
            )
        )
        return
    if any(
        (a.image / name).exists()
        for name in ("scan.json", "scan-summary.json", "scan.log")
    ):
        raise ValueError(
            "Preserve previous scan results; use a new measured build directory"
        )
    with (a.image / "canary.oci.tar").open("rb") as f:
        if hashlib.file_digest(f, "sha256").hexdigest() != image["archive_sha256"]:
            raise ValueError("Built archive changed")
    subprocess.run(
        ["docker", "load", "--input", str(a.image / "canary.oci.tar")],
        capture_output=True,
        check=True,
        timeout=120,
    )
    observed = json.loads(
        subprocess.check_output(
            ["docker", "image", "inspect", image["manifest_digest"]], timeout=30
        )
    )[0]
    user = (
        "70:70"
        if image["kind"] == "db" or image["kind"] == "postgres"
        else "10001:10001"
    )
    if observed["Architecture"] != "arm64" or observed["Config"]["User"] != user:
        raise ValueError(
            "Loaded architecture/user differs from the declared native artifact"
        )
    result = subprocess.run(
        [
            str(a.trivy),
            "image",
            "--image-src",
            "docker",
            "--cache-dir",
            str(a.cache),
            "--skip-db-update",
            "--offline-scan",
            "--scanners",
            "vuln",
            "--parallel",
            "1",
            "--format",
            "json",
            "--output",
            str(a.image / "scan.json"),
            image["manifest_digest"],
        ],
        env={**os.environ, "GOMAXPROCS": "1"},
        capture_output=True,
        timeout=180,
    )
    (a.image / "scan.log").write_bytes(result.stderr)
    result.check_returncode()
    scan = json.loads((a.image / "scan.json").read_text())
    binding = bind_config(image, scan, a.image / "canary.oci.tar")
    vulnerabilities = [
        v for r in scan.get("Results", []) for v in r.get("Vulnerabilities", [])
    ]
    if (
        not scan.get("Results")
        or scan.get("Metadata", {}).get("OS", {}).get("Family") != "alpine"
    ):
        raise ValueError("Expected actual Alpine package scan results required")
    counts = {
        s: sum(v["Severity"] == s for v in vulnerabilities)
        for s in ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL")
    }
    summary = {
        "manifest_digest": image["manifest_digest"],
        "config_digest": binding["config_digest"],
        "config_binding": binding,
        "counts": counts,
        "preflight": "BLOCK" if vulnerabilities else "PASS",
        "observed_at": datetime.now(UTC).isoformat(),
        "vulnerability_db": metadata,
        "vulnerability_db_metadata_sha256": hashlib.sha256(
            metadata_path.read_bytes()
        ).hexdigest(),
        "scanner_sha256": hashlib.sha256(a.trivy.read_bytes()).hexdigest(),
        "scanner_report_sha256": hashlib.sha256(
            (a.image / "scan.json").read_bytes()
        ).hexdigest(),
        "suppression_used": False,
        "deployed": False,
    }
    (a.image / "scan-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(
        json.dumps(
            {
                "manifest": image["manifest_digest"],
                "findings": len(vulnerabilities),
                "preflight": summary["preflight"],
            }
        )
    )


if __name__ == "__main__":
    main()
