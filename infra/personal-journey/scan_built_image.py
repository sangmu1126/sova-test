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


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ("image", "trivy", "cache"):
        p.add_argument("--"+name,type=Path,required=True)
    a=p.parse_args()
    os.umask(0o077)
    metadata_path=a.cache/"db/metadata.json"
    metadata=json.loads(metadata_path.read_text())
    now=datetime.now(UTC)
    if not datetime.fromisoformat(metadata["UpdatedAt"].replace("Z","+00:00"))<=now<datetime.fromisoformat(metadata["NextUpdate"].replace("Z","+00:00")):
        raise ValueError("Current vulnerability database required")
    image=json.loads((a.image/"image.json").read_text())
    if any((a.image/name).exists() for name in ("scan.json","scan-summary.json","scan.log")):
        raise ValueError("Preserve previous scan results; use a new measured build directory")
    with (a.image/"canary.oci.tar").open("rb") as f:
        if hashlib.file_digest(f,"sha256").hexdigest()!=image["archive_sha256"]:
            raise ValueError("Built archive changed")
    subprocess.run(["docker","load","--input",str(a.image/"canary.oci.tar")],capture_output=True,check=True,timeout=120)
    observed=json.loads(subprocess.check_output(["docker","image","inspect",image["manifest_digest"]],timeout=30))[0]
    user="70:70" if image["kind"]=="db" or image["kind"]=="postgres" else "10001:10001"
    if observed["Architecture"]!="arm64" or observed["Config"]["User"]!=user:
        raise ValueError("Loaded architecture/user differs from the declared native artifact")
    result=subprocess.run([str(a.trivy),"image","--image-src","docker","--cache-dir",str(a.cache),
        "--skip-db-update","--offline-scan","--scanners","vuln","--parallel","1","--format","json",
        "--output",str(a.image/"scan.json"),image["manifest_digest"]],env={**os.environ,"GOMAXPROCS":"1"},
        capture_output=True,timeout=180)
    (a.image/"scan.log").write_bytes(result.stderr)
    result.check_returncode()
    scan=json.loads((a.image/"scan.json").read_text())
    vulnerabilities=[v for r in scan.get("Results",[]) for v in r.get("Vulnerabilities",[])]
    if not scan.get("Results") or scan.get("Metadata",{}).get("OS",{}).get("Family")!="alpine":
        raise ValueError("Expected actual Alpine package scan results required")
    counts={s:sum(v["Severity"]==s for v in vulnerabilities) for s in ("UNKNOWN","LOW","MEDIUM","HIGH","CRITICAL")}
    summary={"manifest_digest":image["manifest_digest"],"counts":counts,"preflight":"BLOCK" if vulnerabilities else "PASS",
        "observed_at":datetime.now(UTC).isoformat(),"vulnerability_db":metadata,
        "vulnerability_db_metadata_sha256":hashlib.sha256(metadata_path.read_bytes()).hexdigest(),
        "scanner_sha256":hashlib.sha256(a.trivy.read_bytes()).hexdigest(),
        "scanner_report_sha256":hashlib.sha256((a.image/"scan.json").read_bytes()).hexdigest(),
        "suppression_used":False,"deployed":False}
    (a.image/"scan-summary.json").write_text(json.dumps(summary,indent=2)+"\n")
    print(json.dumps({"manifest":image["manifest_digest"],"findings":len(vulnerabilities),"preflight":summary["preflight"]}))


if __name__=="__main__":
    main()
