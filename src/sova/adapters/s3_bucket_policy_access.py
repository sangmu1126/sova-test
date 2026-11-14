"""Bucket access through statements a handoff owns in an S3 bucket policy.

For S3-compatible stores whose bucket policies name principals and give an
explicit Deny precedence over any Allow: AWS S3, and gateways with the same
policy semantics (measured locally with versitygw accounts).

The handoff names writers logically ("app", "batch"); each store names them
its own way (an IAM role ARN on AWS, an account on a gateway). ``principals``
maps the logical names to this store's principals, so one handoff can fence
the same writer on two providers. Without a mapping the names are used as is.

The handoff owns the statements whose ``Sid`` starts with ``SovaHandoff`` and
never changes any other statement. Every decision is taken from the policy
the provider returns now. The policy is read, changed and written back; S3
offers no conditional write for bucket policies, so an edit by someone else in
between can be lost. The readback then no longer finds the handoff's
statements and reports the action unconfirmed, which is why a handoff never
trusts the write itself.

Evaluation is conservative. A denial counts only if it is unconditional and
covers object writes on every key; a grant counts only if it is unconditional
and nothing that could match denies the principal.
"""

from fnmatch import fnmatchcase
import json
import re

from botocore.exceptions import ClientError

from sova.domain.contracts import ContractError

SID_PREFIX = "SovaHandoff"
WRITER_SID = SID_PREFIX + "Writer"
_WRITES = ("s3:PutObject", "s3:DeleteObject")
_OBJECT_ACCESS = ("s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket",
                  "s3:GetObjectTagging", "s3:PutObjectTagging", "s3:DeleteObjectTagging",
                  "s3:ListBucketMultipartUploads", "s3:AbortMultipartUpload",
                  "s3:ListMultipartUploadParts")
_NAME = re.compile(r"^[A-Za-z0-9]{1,48}$")


def _list(value):
    return [value] if isinstance(value, str) else list(value or ())


def _principals(statement):
    principal = statement.get("Principal")
    if principal == "*":
        return ["*"]
    if isinstance(principal, dict):
        return _list(principal.get("AWS"))
    return []


def _covers(patterns, value):
    """A literal ``value`` falls under one of the patterns; '?' and character
    classes are not treated as covering everything."""
    return any("?" not in p and "[" not in p and fnmatchcase(value.lower(), p.lower())
               for p in _list(patterns))


class S3BucketPolicyAccess:
    def __init__(self, client, bucket, *, principals=None):
        if not callable(getattr(client, "get_bucket_policy", None)):
            raise ContractError("S3 API client required")
        if principals is not None and (type(principals) is not dict or not all(
                type(k) is str and type(v) is str and v for k, v in principals.items())):
            raise ContractError("Logical writer names mapped to principals required")
        self.client, self.bucket = client, bucket
        self.principals = principals
        self.arn = f"arn:aws:s3:::{bucket}"
        self.objects = self.arn + "/*"

    def _p(self, name):
        if self.principals is None:
            return name
        if name not in self.principals:
            raise ContractError("Writer has no principal on this store")
        return self.principals[name]

    # -- policy document --------------------------------------------------------
    def _policy(self):
        try:
            text = self.client.get_bucket_policy(Bucket=self.bucket)["Policy"]
        except ClientError as error:
            code = error.response["Error"]["Code"]
            if code == "NoSuchBucketPolicy":
                return {"Version": "2012-10-17", "Statement": []}
            if code == "NoSuchBucket":
                return None
            raise
        document = json.loads(text)
        statements = document.get("Statement", [])
        document["Statement"] = [statements] if isinstance(statements, dict) else list(
            statements)
        return document

    def _existing(self):
        document = self._policy()
        if document is None:
            raise ContractError("Bucket does not exist")
        return document

    def _store(self, document):
        if document["Statement"]:
            self.client.put_bucket_policy(Bucket=self.bucket,
                                          Policy=json.dumps(document, sort_keys=True))
        else:
            self.client.delete_bucket_policy(Bucket=self.bucket)

    @staticmethod
    def _put(statements, statement):
        """Replace the statement with the same Sid in place, or append it."""
        for i, existing in enumerate(statements):
            if existing.get("Sid") == statement["Sid"]:
                statements[i] = statement
                return
        statements.append(statement)

    # -- BucketAccess ------------------------------------------------------------
    def deny(self, name, principals):
        if not _NAME.match(name or "") or not principals:
            raise ContractError("Alphanumeric statement name and principals required")
        document = self._existing()
        self._put(document["Statement"], {
            "Sid": SID_PREFIX + name, "Effect": "Deny",
            "Principal": {"AWS": sorted(self._p(p) for p in principals)}, "Action": ["s3:*"],
            "Resource": [self.arn, self.objects]})
        self._store(document)

    def open_writer(self, principal):
        principal = self._p(principal)
        document = self._existing()
        kept = []
        for statement in document["Statement"]:
            if (str(statement.get("Sid", "")).startswith(SID_PREFIX)
                    and statement.get("Effect") == "Deny"):
                others = [p for p in _principals(statement) if p != principal]
                if not others:
                    continue
                statement = {**statement, "Principal": {"AWS": sorted(others)}}
            kept.append(statement)
        self._put(kept, {
            "Sid": WRITER_SID, "Effect": "Allow", "Principal": {"AWS": [principal]},
            "Action": list(_OBJECT_ACCESS), "Resource": [self.arn, self.objects]})
        document["Statement"] = kept
        self._store(document)

    def _matches(self, statement, principal):
        names = _principals(statement)
        return ("*" in names or principal in names) and not any(
            k in statement for k in ("NotPrincipal", "NotAction", "NotResource"))

    def denied(self, principals, *, by=None):
        """``by`` limits the decision to the handoff's own statement of that name."""
        document = self._policy()
        statements = [] if document is None else [
            s for s in document["Statement"]
            if s.get("Effect") == "Deny" and "Condition" not in s
            and (by is None or s.get("Sid") == SID_PREFIX + by)
            and all(_covers(s.get("Action"), a) for a in _WRITES)
            and _covers(s.get("Resource"), self.objects)]
        names = {p: self._p(p) for p in principals}  # an unknown writer is an error
        return {p: any(self._matches(s, name) for s in statements) for p, name in names.items()}

    def _touches_objects(self, resources):
        return any(r == "*" or _covers(r, self.objects)
                   or r.lower().startswith(self.arn.lower() + "/") for r in _list(resources))

    def writable_by(self, principal):
        principal = self._p(principal)
        document = self._policy()
        if document is None:
            return False
        for s in document["Statement"]:
            if s.get("Effect") != "Deny":
                continue
            names = _principals(s)
            if "NotPrincipal" in s or "*" in names or principal in names:
                # A denial that could reach any of this principal's object writes,
                # conditional or partial, or one we cannot bound, rules it out.
                if any(k in s for k in ("NotAction", "NotResource")) or (
                        any(_covers(s.get("Action"), a) for a in _WRITES)
                        and self._touches_objects(s.get("Resource"))):
                    return False
        return any(
            s.get("Effect") == "Allow" and "Condition" not in s and self._matches(s, principal)
            and all(_covers(s.get("Action"), a) for a in _WRITES)
            and _covers(s.get("Resource"), self.objects)
            for s in document["Statement"])

    def policy_text(self):
        """The current policy as the provider returns it (for observation)."""
        document = self._policy()
        return None if document is None else json.dumps(document, sort_keys=True)
