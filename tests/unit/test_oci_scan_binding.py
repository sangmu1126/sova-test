"""OCI identity binding tests; no actual scanner/registry qualification."""

import hashlib
import io
import json
import tarfile

import pytest

from scan_built_image import bind_config


def fixture(tmp_path):
    diffs = ["sha256:" + "c" * 64]
    raw = json.dumps(
        {"architecture": "arm64", "rootfs": {"type": "layers", "diff_ids": diffs}}
    ).encode()
    config = "sha256:" + hashlib.sha256(raw).hexdigest()
    image = {"manifest_digest": "sha256:" + "a" * 64, "config_digest": config}
    archive = tmp_path / "image.tar"
    with tarfile.open(archive, "w") as t:
        entry = tarfile.TarInfo("blobs/sha256/" + config[7:])
        entry.size = len(raw)
        t.addfile(entry, io.BytesIO(raw))
    return image, archive, diffs


@pytest.mark.parametrize("identity", ("manifest_digest", "config_digest"))
def test_manifest_and_config_runtime_ids_still_require_exact_content_layers(
    tmp_path, identity
):
    image, archive, diffs = fixture(tmp_path)
    result = bind_config(
        image, {"Metadata": {"ImageID": image[identity], "DiffIDs": diffs}}, archive
    )
    assert result["config_digest"] == image["config_digest"]
    assert (
        result["scanner_image_id"] == image[identity]
        and result["diff_ids_verified"] == 1
    )


@pytest.mark.parametrize("mutation", ("image_id", "diff_ids", "missing_layers"))
def test_wrong_or_missing_scanner_identity_is_refused(tmp_path, mutation):
    image, archive, diffs = fixture(tmp_path)
    metadata = {"ImageID": image["manifest_digest"], "DiffIDs": diffs}
    if mutation == "image_id":
        metadata["ImageID"] = "sha256:" + "b" * 64
    elif mutation == "diff_ids":
        metadata["DiffIDs"] = ["sha256:" + "d" * 64]
    else:
        metadata["DiffIDs"] = []
    with pytest.raises(ValueError):
        bind_config(image, {"Metadata": metadata}, archive)


def test_config_bytes_cannot_be_replaced_under_the_same_blob_name(tmp_path):
    image, archive, diffs = fixture(tmp_path)
    raw = b'{"rootfs":{"diff_ids":[]}}'
    with tarfile.open(archive, "w") as t:
        entry = tarfile.TarInfo("blobs/sha256/" + image["config_digest"][7:])
        entry.size = len(raw)
        t.addfile(entry, io.BytesIO(raw))
    with pytest.raises(ValueError, match="config blob"):
        bind_config(
            image,
            {"Metadata": {"ImageID": image["manifest_digest"], "DiffIDs": diffs}},
            archive,
        )
