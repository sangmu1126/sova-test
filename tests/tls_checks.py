"""Transport refusal checks against a store fixture's TLS endpoint."""

from pathlib import Path

from sova.adapters.tls_refusal import TlsEndpoint


def endpoint(port, ca_file, **extra):
    return TlsEndpoint("127.0.0.1", port, "localhost", Path(ca_file).read_bytes(), **extra)


def summary(observation):
    return observation["outcome"], {k: (v.get("outcome"), v.get("reason"), v.get("verify_code"))
                                    for k, v in observation["checks"].items()}


def identity_held(observation):
    """Verified before and after; a wrong CA and a wrong host name refused."""
    checks = observation["checks"]
    return (checks["positive_before"]["outcome"] == checks["positive_after"]["outcome"]
            == "verified" and checks["wrong_ca"]["outcome"] == "refused"
            and checks["wrong_host"]["outcome"] == "refused")
