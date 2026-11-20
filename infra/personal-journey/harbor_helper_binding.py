"""Operator-bound qualification helper image; never arbitrary task image selection."""

from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path
import re

from sova.adapters.kubernetes.https import private_text
from sova.domain.contracts import ContractError, utc
from sova.domain.json_documents import json_document


def qualified_helper(path, image_inspect, *, now=None):
    now = utc(now or datetime.now(UTC))
    config = json_document(private_text(path), 65536)
    if (
        type(config) is not dict
        or set(config)
        != {
            "version",
            "image_id",
            "scan_file",
            "scan_sha256",
            "database_file",
            "database_sha256",
        }
        or type(config["version"]) is not int
        or config["version"] != 1
    ):
        raise ContractError("Exact private helper configuration required")
    if not isinstance(config["image_id"], str) or not re.fullmatch(
        r"sha256:[a-f0-9]{64}", config["image_id"]
    ):
        raise ContractError("Exact local helper image identity required")
    records = {}
    for kind in ("scan", "database"):
        if (
            type(config[kind + "_file"]) is not str
            or not Path(config[kind + "_file"]).is_absolute()
        ):
            raise ContractError("Operator selected absolute helper receipt required")
        raw = private_text(config[kind + "_file"], limit=4 << 20)
        if hashlib.sha256(raw.encode()).hexdigest() != config[kind + "_sha256"]:
            raise ContractError("Helper inspection receipt changed")
        records[kind] = json_document(raw, 4 << 20)
    scan, db = records["scan"], records["database"]
    if (
        scan.get("ArtifactType") != "container_image"
        or scan.get("Metadata", {}).get("ImageID") != config["image_id"]
        or not scan.get("Results")
        or any(row.get("Vulnerabilities") for row in scan["Results"])
    ):
        raise ContractError("Helper image has unqualified or vulnerable scan results")
    created = utc(datetime.fromisoformat(scan["CreatedAt"]))
    updated = utc(datetime.fromisoformat(db["UpdatedAt"].replace("Z", "+00:00")))
    next_update = utc(datetime.fromisoformat(db["NextUpdate"].replace("Z", "+00:00")))
    if not (
        updated <= created <= now < next_update
        and next_update - updated <= timedelta(hours=48)
        and now - created <= timedelta(hours=24)
    ):
        raise ContractError("Fresh helper database and non-future inspection required")
    image = image_inspect(config["image_id"])
    if (
        image["Id"] != config["image_id"]
        or image["Architecture"] != "arm64"
        or image["RootFS"]["Layers"] != scan["Metadata"]["DiffIDs"]
        or not image["RootFS"]["Layers"]
    ):
        raise ContractError("Actual helper image architecture or layers differ")
    return config["image_id"]
