"""Strict, bounded graph interchange. Reject unknown keys and duplicate JSON."""

from datetime import datetime
from pathlib import Path

from sova.domain.contracts import ContractError, ResourceScope
from sova.domain.json_documents import json_document as json_document
from sova.domain.system_graph import (
    Portability,
    RelationKind,
    ResourceKind,
    SystemGraph,
    SystemRelation,
    SystemResource,
)

MAX_GRAPH_BYTES = 262144


def read_document(path, limit=MAX_GRAPH_BYTES):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ContractError("A regular local registration file is required")
    with path.open("rb") as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ContractError("Registration size limit exceeded")
    try:
        return json_document(raw.decode("utf-8"), limit)
    except UnicodeError as exc:
        raise ContractError("UTF-8 registration required") from exc


def exact(data, keys):
    if not isinstance(data, dict) or set(data) != set(keys):
        raise ContractError("Registration fields do not match schema")


def decode_graph(data):
    try:
        exact(
            data,
            (
                "schema_version",
                "tenant_id",
                "application_id",
                "revision",
                "origin",
                "observed_at",
                "valid_until",
                "resources",
                "relations",
            ),
        )
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ContractError("Unsupported graph schema")
        if not isinstance(data["resources"], list) or not isinstance(
            data["relations"], list
        ):
            raise ContractError("Resource/relation lists required")
        resources = []
        for node in data["resources"]:
            exact(
                node,
                (
                    "id",
                    "name",
                    "scope",
                    "kind",
                    "backend",
                    "location",
                    "resource_revision",
                    "portability",
                    "requirements",
                ),
            )
            exact(
                node["scope"],
                ("tenant_id", "application_id", "environment_id", "resource_id"),
            )
            if not isinstance(node["requirements"], list):
                raise ContractError("Requirements list required")
            resources.append(
                SystemResource(
                    node["id"],
                    node["name"],
                    ResourceScope(**node["scope"]),
                    ResourceKind(node["kind"]),
                    node["backend"],
                    node["location"],
                    node["resource_revision"],
                    Portability(node["portability"]),
                    tuple(node["requirements"]),
                )
            )
        relations = []
        for edge in data["relations"]:
            exact(edge, ("source", "target", "kind", "required"))
            relations.append(
                SystemRelation(
                    edge["source"],
                    edge["target"],
                    RelationKind(edge["kind"]),
                    edge["required"],
                )
            )
        return SystemGraph(
            data["tenant_id"],
            data["application_id"],
            data["revision"],
            data["origin"],
            datetime.fromisoformat(data["observed_at"]),
            datetime.fromisoformat(data["valid_until"]),
            tuple(resources),
            tuple(relations),
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ContractError("Invalid system graph registration") from exc
