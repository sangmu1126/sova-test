"""Writer access to one object-storage bucket, as a handoff needs it.

Object stores differ in how access is expressed (S3 bucket policies, cloud IAM
bindings, gateway accounts), but a handoff needs the same four things from
each. Implementations change and read the provider's real access state; they
never answer from memory.
"""

from typing import Protocol


class BucketAccess(Protocol):
    def deny(self, name: str, principals: tuple[str, ...]) -> None:
        """Deny ``principals`` every request on the bucket and its objects, under
        a statement ``name`` the handoff owns. Idempotent; leaves every other
        access rule as it was."""
        ...

    def open_writer(self, principal: str) -> None:
        """Let ``principal`` read and write objects again: lift the handoff's own
        denials for it (others stay denied) and grant object read/write."""
        ...

    def denied(self, principals: tuple[str, ...]) -> dict[str, bool]:
        """Per principal: True only if an unconditional denial of all object
        writes is in force now."""
        ...

    def writable_by(self, principal: str) -> bool:
        """True only if the access state now grants ``principal`` object writes
        and nothing denies them."""
        ...
