"""Compare a cached service image's installed source with two fixed Git trees.

This is a local preflight, not signature verification, an SBOM vulnerability
scan, deployment approval or proof that a Pod runs the image. No tag is accepted
and no image is pulled. Source labels and package versions alone are not proof.
"""

import argparse
from datetime import UTC, datetime
import hashlib
import io
import json
import os
from pathlib import Path
import posixpath
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from uuid import uuid4


IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
REVISION = re.compile(r"[0-9a-f]{40}\Z")
PACKAGES = {"sova": "sova-control-plane", "sova_security": "sova-security"}
SUFFIXES = {".py", ".sql", ".so", ".pyd"}

# Deliberately do not import either application. Read the installed package
# payload and distribution metadata using only the image's standard library.
PROBE = r'''
import hashlib, importlib.metadata as m, json, pathlib, platform, sys
out = {"python": platform.python_version(), "packages": {}, "distributions": {}, "entrypoints": {}}
for pkg, name in {"sova": "sova-control-plane", "sova_security": "sova-security"}.items():
    d = m.distribution(name)
    root = pathlib.Path(d.locate_file(pkg))
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError("package_root_invalid")
    files, total = {}, 0
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            raise RuntimeError("package_symlink")
        if not p.is_file() or p.suffix not in {".py", ".sql", ".so", ".pyd"}:
            continue
        size = p.stat().st_size
        total += size
        if size > 8*1024*1024 or total > 64*1024*1024 or len(files) >= 2000:
            raise RuntimeError("package_inventory_limit")
        files[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    out["packages"][pkg] = {"version": d.version, "files": files}
    out["entrypoints"][name] = {e.name: e.value for e in d.entry_points if e.group == "console_scripts"}
for d in m.distributions():
    name = d.metadata["Name"]
    if name in out["distributions"]:
        raise RuntimeError("duplicate_distribution")
    out["distributions"][name] = d.version
print(json.dumps(out, sort_keys=True))
'''


class ImageCheckError(ValueError):
    """A safe error code, without command output or local secret contents."""


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL)


def source_inventory(root, revision, package):
    if not REVISION.fullmatch(revision) or package not in PACKAGES:
        raise ImageCheckError("source_reference_invalid")
    prefix = f"src/{package}/"
    try:
        resolved = git(root, "rev-parse", "--verify", revision + "^{commit}").decode().strip()
        if resolved != revision:
            raise ImageCheckError("source_reference_invalid")
        data = git(root, "archive", "--format=tar", revision, prefix)
    except subprocess.CalledProcessError:
        raise ImageCheckError("source_reference_unavailable") from None
    expected, links = {}, {}
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        members = {m.name: m for m in archive}
        for member in members.values():
            if not member.name.startswith(prefix) or member.isdir():
                continue
            relative = member.name[len(prefix):]
            if member.issym():
                # setuptools materializes the repository's internal SQL
                # aliases in the wheel. Resolve only a Git-declared link to
                # a regular file inside this same fixed package tree.
                target = posixpath.normpath(posixpath.join(posixpath.dirname(member.name), member.linkname))
                resolved = members.get(target)
                if (posixpath.isabs(member.linkname) or not target.startswith(prefix)
                        or resolved is None or not resolved.isfile()):
                    raise ImageCheckError("source_symlink_or_special_file")
                links[relative] = member.linkname
                member = resolved
            elif not member.isfile():
                raise ImageCheckError("source_symlink_or_special_file")
            if Path(relative).suffix not in SUFFIXES:
                continue
            expected[relative] = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
    if not expected:
        raise ImageCheckError("source_package_empty")
    current = {}
    package_root = root / "src" / package
    if package_root.is_symlink():
        raise ImageCheckError("source_symlink_or_special_file")
    for path in package_root.rglob("*"):
        relative = path.relative_to(package_root).as_posix()
        if path.is_symlink():
            if (links.get(relative) != os.readlink(path)
                    or not path.resolve().is_relative_to(package_root.resolve())
                    or not path.is_file()):
                raise ImageCheckError("source_symlink_or_special_file")
        elif relative in links:
            raise ImageCheckError("source_symlink_or_special_file")
        if path.is_file() and path.suffix in SUFFIXES:
            current[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    if current != expected:
        raise ImageCheckError("working_source_differs_from_revision")
    return expected


def project_version(root, revision):
    try:
        return tomllib.loads(git(root, "show", revision + ":pyproject.toml").decode())["project"]["version"]
    except (subprocess.CalledProcessError, ValueError, KeyError):
        raise ImageCheckError("source_project_metadata_invalid") from None


def compare(expected, observed, versions):
    """Missing, extra and changed code/data all refuse a source match."""
    if (type(observed) is not dict or type(observed.get("packages")) is not dict
            or set(observed["packages"]) != set(expected)
            or type(observed.get("entrypoints")) is not dict
            or type(observed.get("distributions")) is not dict):
        raise ImageCheckError("image_inventory_invalid")
    result = {}
    for package, files in expected.items():
        item = observed.get("packages", {}).get(package, {})
        if type(item) is not dict:
            raise ImageCheckError("image_inventory_invalid")
        actual = item.get("files")
        if (type(actual) is not dict or not actual
                or any(type(k) is not str or type(v) is not str
                       or not re.fullmatch(r"[0-9a-f]{64}", v) for k, v in actual.items())):
            raise ImageCheckError("image_inventory_invalid")
        result[package] = {
            "expected_files": len(files), "installed_files": len(actual),
            "missing": sorted(files.keys() - actual.keys()),
            "extra": sorted(actual.keys() - files.keys()),
            "changed": sorted(k for k in files.keys() & actual.keys() if files[k] != actual[k]),
            "version_matches": type(item.get("version")) is str and item["version"] == versions[package],
        }
    entries = observed.get("entrypoints", {}).get("sova-control-plane", {})
    if type(entries) is not dict:
        raise ImageCheckError("image_inventory_invalid")
    entrypoint_matches = entries.get("sova-service") == "sova.service.main:main"
    matches = entrypoint_matches and all(
        not r["missing"] and not r["extra"] and not r["changed"] and r["version_matches"]
        for r in result.values())
    return {"status": "MATCH" if matches else "MISMATCH", "packages": result,
            "service_entrypoint_matches": entrypoint_matches}


def inspect_image(image, architecture):
    if not IMAGE_ID.fullmatch(image) or architecture not in {"arm64", "amd64"}:
        raise ImageCheckError("immutable_image_and_architecture_required")
    try:
        info = json.loads(subprocess.check_output(
            ["docker", "image", "inspect", image], stderr=subprocess.DEVNULL, timeout=20))[0]
    except (subprocess.SubprocessError, ValueError, IndexError):
        raise ImageCheckError("cached_image_unavailable") from None
    if info.get("Id") != image or info.get("Architecture") != architecture or info.get("Os") != "linux":
        raise ImageCheckError("image_identity_or_architecture_mismatch")
    config = info.get("Config", {})
    user = config.get("User", "")
    if (user not in {"10001", "10001:10001"}
            or config.get("Entrypoint") != ["sova-service"]):
        raise ImageCheckError("service_image_runtime_config_invalid")
    return {"image_id": image, "architecture": architecture, "os": "linux",
            "user": user, "entrypoint": config["Entrypoint"]}


def probe(image):
    name = "sova-image-audit-" + uuid4().hex
    # No host mounts, published ports, cloud credentials, daemon socket or
    # network. The image's normal service entrypoint is not invoked here.
    command = ["docker", "run", "--rm", "--pull", "never", "--name", name,
               "--label", "sova.image-audit=" + name, "--network", "none",
               "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--pids-limit", "32", "--memory", "256m", "--cpus", "1",
               "--user", "10001:10001", "--entrypoint", "python", image, "-I", "-B", "-c", PROBE]
    try:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
            completed = subprocess.run(command, stdout=output, stderr=error, timeout=90)
            if completed.returncode or output.tell() > 4 * 1024 * 1024:
                raise ImageCheckError("image_probe_failed")
            output.seek(0)
            try:
                return json.load(output)
            except ValueError:
                raise ImageCheckError("image_inventory_invalid") from None
    except subprocess.TimeoutExpired:
        raise ImageCheckError("image_probe_timed_out") from None
    finally:
        # docker run --rm handles normal exit. A timed-out Docker CLI may
        # leave a container: remove only the exact label-owned container.
        found = subprocess.run(["docker", "inspect", "--format",
                                '{{index .Config.Labels "sova.image-audit"}}', name],
                               capture_output=True, text=True, timeout=20)
        if found.returncode == 0 and found.stdout.strip() == name:
            subprocess.run(["docker", "rm", "--force", name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=True, timeout=20)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--architecture", required=True, choices=("arm64", "amd64"))
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--security-source", required=True, type=Path)
    parser.add_argument("--security-revision", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    report = {"schema_version": "service-image-source.v1", "observed_at": datetime.now(UTC).isoformat(),
              "qualification_issued": False, "authorizes_execution": False,
              "deployment_observed": False, "source_revision": args.source_revision,
              "security_revision": args.security_revision}
    # Exclusive creation avoids replacing earlier evidence, even after an error.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    code = 1
    try:
        report.update(inspect_image(args.image, args.architecture))
        expected = {"sova": source_inventory(args.source, args.source_revision, "sova"),
                    "sova_security": source_inventory(args.security_source, args.security_revision, "sova_security")}
        observed = probe(args.image)
        versions = {"sova": project_version(args.source, args.source_revision),
                    "sova_security": project_version(args.security_source, args.security_revision)}
        report.update(compare(expected, observed, versions))
        report["installed"] = observed
        report["expected_source_sha256"] = expected
        code = 0 if report["status"] == "MATCH" else 2
    except (ImageCheckError, subprocess.SubprocessError, OSError) as exc:
        report.update(status="ERROR", error=str(exc) if isinstance(exc, ImageCheckError) else "inspection_failed")
    finally:
        with os.fdopen(fd, "w") as out:
            json.dump(report, out, indent=2, sort_keys=True)
            out.write("\n")
    print(json.dumps({"status": report["status"], "qualification_issued": False,
                      "authorizes_execution": False}))
    return code


if __name__ == "__main__":
    sys.exit(main())
