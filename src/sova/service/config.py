"""Deployment configuration from the environment and mounted secret files.

Secrets are never values in the environment: each ``*_FILE`` variable names a
file (a mounted Kubernetes Secret) that holds a DSN, a key or credentials.

    SOVA_DATABASE_URL_FILE         runtime role DSN (API, worker, admin)
    SOVA_MIGRATION_DATABASE_URL_FILE  schema owner DSN (migrate job only)
    SOVA_RUNTIME_ROLE              the role migrate grants to
    SOVA_OIDC_ISSUER, SOVA_OIDC_AUDIENCE, SOVA_OIDC_JWKS_URL
    SOVA_TENANT_CLAIM, SOVA_ROLES_CLAIM, SOVA_ROLE_PREFIX   (defaults below)
    SOVA_REGISTRY_VERIFIER_VERSION, SOVA_EVIDENCE_VERIFIER, SOVA_EVIDENCE_ISSUER
    SOVA_WITNESS                   file:<path> | s3://<bucket>  (authority witness)
    SOVA_WITNESS_S3_FILE           endpoint, region and credentials for s3://
    SOVA_TENANTS                   comma-separated tenants a worker serves
    SOVA_EVIDENCE_KEY_FILE         the worker's Ed25519 evidence key (hex, 32 bytes)
    SOVA_EVIDENCE_KEY_ID           its key ID in the verifier registry
    SOVA_EXECUTION_FILE            the worker's adapter catalog (``catalog.py``)
    SOVA_LEASE_SECONDS             execution lease, 10..300 seconds (default 120)
    SOVA_WORKER_STALL_SECONDS      no progress this long fails liveness (default 600)
    POD_NAME, POD_UID, POD_NAMESPACE  from the downward API (worker owner identity)

The JWKS is fetched over HTTPS. Plain HTTP is accepted only for a loopback
test issuer (``http://127.0.0.1...``), the one exception the authenticator
itself makes.
"""

from dataclasses import dataclass
from datetime import timedelta
import json
import os
from pathlib import Path
import secrets

from sova.domain.contracts import ContractError


def _secret(name, env=os.environ):
    path = env.get(name)
    if not path:
        raise ContractError(f"{name} must name a mounted secret file")
    text = Path(path).read_text().strip()
    if not text:
        raise ContractError(f"{name} names an empty file")
    return text


def _required(name, env=os.environ):
    value = env.get(name)
    if not value:
        raise ContractError(f"{name} is required")
    return value


def _seconds(name, default, env):
    value = env.get(name, str(default))
    if (type(value) is not str or not 1 <= len(value) <= 10
            or not value.isascii() or not value.isdecimal()):
        raise ContractError(f"{name} must be unsigned decimal seconds")
    return int(value)


@dataclass(frozen=True)
class ServiceConfig:
    database_url: str
    issuer: str
    audience: str
    jwks_url: str
    tenant_claim: str
    roles_claim: str
    role_prefix: str
    registry_verifier_version: str
    evidence_verifier: str
    evidence_issuer: str
    witness: str
    witness_s3: dict

    @classmethod
    def from_env(cls, env=os.environ):
        witness = _required("SOVA_WITNESS", env)
        issuer, jwks = _required("SOVA_OIDC_ISSUER", env), _required("SOVA_OIDC_JWKS_URL", env)
        if not jwks.startswith("https://") and not issuer.startswith("http://127.0.0.1"):
            raise ContractError("The JWKS must be fetched over HTTPS")
        return cls(
            database_url=_secret("SOVA_DATABASE_URL_FILE", env),
            issuer=issuer,
            audience=_required("SOVA_OIDC_AUDIENCE", env),
            jwks_url=jwks,
            tenant_claim=env.get("SOVA_TENANT_CLAIM", "sova_tenant"),
            roles_claim=env.get("SOVA_ROLES_CLAIM", "realm_access.roles"),
            role_prefix=env.get("SOVA_ROLE_PREFIX", "sova:"),
            registry_verifier_version=_required("SOVA_REGISTRY_VERIFIER_VERSION", env),
            evidence_verifier=_required("SOVA_EVIDENCE_VERIFIER", env),
            evidence_issuer=_required("SOVA_EVIDENCE_ISSUER", env),
            witness=witness,
            witness_s3=(json.loads(_secret("SOVA_WITNESS_S3_FILE", env))
                        if witness.startswith("s3://") else {}))

    def witness_for(self):
        """tenant -> the authority witness every process of this deployment shares."""
        if self.witness.startswith("file:"):
            from sova.adapters.file_authority_witness import FileAuthorityWitness

            root = Path(self.witness[len("file:"):])
            return lambda tenant: FileAuthorityWitness(root / f"{tenant}.log", tenant_id=tenant)
        if self.witness.startswith("s3://"):
            import boto3
            from botocore.config import Config

            from sova.adapters.s3_authority_witness import S3ObjectLockAuthorityWitness

            s3 = self.witness_s3
            if set(s3) - {"endpoint", "region", "access_key_id", "secret_access_key",
                          "addressing_style", "retention_seconds", "mode"}:
                raise ContractError("Unknown witness store settings")
            # Without keys the pod's own workload identity is used (IRSA, Workload Identity).
            keys = ({"aws_access_key_id": s3["access_key_id"],
                     "aws_secret_access_key": s3["secret_access_key"]}
                    if s3.get("access_key_id") else {})
            client = boto3.client(
                "s3", endpoint_url=s3.get("endpoint"), region_name=s3.get("region", "us-east-1"),
                config=Config(connect_timeout=5, read_timeout=15,
                              retries={"max_attempts": 3, "mode": "standard"},
                              s3={"addressing_style": s3.get("addressing_style", "auto")}),
                **keys)
            bucket = self.witness[len("s3://"):].strip("/")
            return lambda tenant: S3ObjectLockAuthorityWitness(
                client, bucket, tenant_id=tenant,
                retention=timedelta(seconds=int(s3.get("retention_seconds", 86400))),
                mode=s3.get("mode", "COMPLIANCE"))
        raise ContractError("SOVA_WITNESS is file:<path> or s3://<bucket>")


@dataclass(frozen=True)
class WorkerConfig:
    tenants: tuple
    evidence_key: bytes
    evidence_key_id: str
    execution: dict
    lease_seconds: int
    stall_seconds: int
    pod_name: str
    pod_uid: str
    pod_namespace: str
    instance: str  # new at every process start: a restarted container is a new owner

    @classmethod
    def from_env(cls, env=os.environ):
        tenants = tuple(t.strip() for t in _required("SOVA_TENANTS", env).split(",") if t.strip())
        if not tenants:
            raise ContractError("SOVA_TENANTS names no tenant")
        key = bytes.fromhex(_secret("SOVA_EVIDENCE_KEY_FILE", env))
        if len(key) != 32:
            raise ContractError("The evidence key is 32 bytes of hex")
        lease = _seconds("SOVA_LEASE_SECONDS", 120, env)
        # The control ledger is PostgreSQL even when the business adapter is
        # another engine. Reuse its actual bound instead of duplicating an
        # upper limit which could let an unexecutable worker start.
        from sova.adapters.postgres_execution import PostgresExecutionOwnership

        try:
            PostgresExecutionOwnership.duration(lease)
        except ContractError:
            raise ContractError("SOVA_LEASE_SECONDS is outside execution ledger bounds") from None
        if lease < 10:
            raise ContractError("SOVA_LEASE_SECONDS must be at least 10 seconds")
        stall = _seconds("SOVA_WORKER_STALL_SECONDS", 600, env)
        if not lease < stall <= 86400:
            raise ContractError("SOVA_WORKER_STALL_SECONDS must exceed the lease")
        return cls(tenants, key, _required("SOVA_EVIDENCE_KEY_ID", env),
                   json.loads(_secret("SOVA_EXECUTION_FILE", env)), lease, stall,
                   _required("POD_NAME", env), _required("POD_UID", env),
                   _required("POD_NAMESPACE", env), secrets.token_hex(6))

    @property
    def owner_id(self):
        return f"{self.pod_name}.{self.pod_uid}.{self.instance}"
