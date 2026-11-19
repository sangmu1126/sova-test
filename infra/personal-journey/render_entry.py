"""Prepare a standard HAProxy entry, preserving real backend identity headers.

Private output contains an entry leaf key. No app token, proxy retry, forged
side/digest response, traffic switch or deployment is performed here.
"""

import argparse
import base64
from datetime import UTC, datetime, timedelta
import ipaddress
import json
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("inputs", "connections", "image-proof", "services", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    args = p.parse_args()
    os.umask(0o077)
    identity = json.loads((args.inputs / "identity.json").read_text())
    links, images, services = [json.loads(path.read_text()) for path in
                              (args.connections, args.image_proof, args.services)]
    run, ns = identity["run_id"], identity["namespace"]
    if run != "b0be270e62" or links["run_id"] != run or images["run_id"] != run or not links["ready"]:
        raise ValueError("Run-owned actual connection/image inputs required")
    source_port = links["ports"]["source_app"]
    if type(source_port) is not int or not 1024 < source_port < 65536:
        raise ValueError("Bounded existing SSM port required")
    target = next(s for s in services["items"] if s["metadata"]["name"] == "orders-app")
    if target["metadata"]["namespace"] != ns or target["metadata"]["labels"].get("sova.run") != run:
        raise ValueError("Wrong target application Service")
    target_ip = str(ipaddress.ip_address(target["spec"]["clusterIP"]))
    now, expires = datetime.now(UTC), datetime.fromisoformat(identity["expires_at"])
    if now >= expires:
        raise ValueError("Deployment credentials expired")
    args.output.mkdir(mode=0o700, exist_ok=False)
    key = ec.generate_private_key(ec.SECP256R1())
    ca = x509.load_pem_x509_certificate((args.inputs / "pki/ca.pem").read_bytes())
    cakey = serialization.load_pem_private_key((args.inputs / "pki/ca-key.pem").read_bytes(), password=None)
    name = "entry." + run + ".sova.invalid"
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(expires)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(cakey.public_key()), critical=False)
        .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False).sign(cakey, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM) + key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    (args.output / "entry.pem").write_bytes(pem)
    labels = {"sova.run": run, "sova.scope": "personal-qualification"}
    resources = []

    def add(kind, name, *, api="v1", **values):
        resources.append({"apiVersion": api, "kind": kind,
            "metadata": {"name": name, "namespace": ns, "labels": labels}, **values})

    add("Secret", "orders-entry-tls", immutable=True, type="Opaque", data={
        "entry.pem": base64.b64encode(pem).decode(),
        "ca.pem": base64.b64encode((args.inputs / "pki/ca.pem").read_bytes()).decode()})
    add("ServiceAccount", "orders-entry", automountServiceAccountToken=False)
    add("NetworkPolicy", "entry-registered-upstreams", api="networking.k8s.io/v1", spec={
        "podSelector": {"matchLabels": {"sova.role": "edge"}}, "policyTypes": ["Ingress", "Egress"],
        "ingress": [{"from": [{"podSelector": {"matchLabels": {"sova.role": "qualifier"}}}],
            "ports": [{"protocol": "TCP", "port": 8443}]}],
        "egress": [{"to": [{"ipBlock": {"cidr": "192.168.5.2/32"}}],
                    "ports": [{"protocol": "TCP", "port": source_port}]},
                   {"to": [{"podSelector": {"matchLabels": {"sova.role": "app"}}}],
                    "ports": [{"protocol": "TCP", "port": 8080}]}]})
    for side, address, port in (("source", "192.168.5.2", source_port), ("target", target_ip, 8080)):
        host = identity["environments"][side]["application_host"]
        configuration = f'''global
  maxconn 64
  nbthread 1
  ssl-default-bind-options ssl-min-ver TLSv1.2
  ssl-default-server-options ssl-min-ver TLSv1.2
defaults
  mode http
  timeout connect 3s
  timeout client 15s
  timeout server 10s
  retries 0
frontend entry
  bind :8443 ssl crt /run/tls/entry.pem
  default_backend actual_application
backend actual_application
  option httpchk
  http-check send meth GET uri /healthz ver HTTP/1.1 hdr Host {host}
  http-check expect status 200
  server actual {address}:{port} ssl verify required ca-file /run/tls/ca.pem verifyhost {host} sni str({host}) check check-sni {host} inter 2s
'''
        (args.output / (side + "-haproxy.cfg")).write_text(configuration)
        name = "orders-entry-" + side
        add("ConfigMap", name, immutable=True, data={"haproxy.cfg": configuration})
        selector = {"sova.run": run, "sova.role": "edge", "sova.side": side}
        add("Deployment", name, api="apps/v1", spec={"replicas": 1, "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": selector}, "template": {"metadata": {"labels": selector}, "spec": {
                "serviceAccountName": "orders-entry", "automountServiceAccountToken": False,
                "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                    "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{"name": "haproxy", "image": images["images"]["proxy"]["reference"],
                    "imagePullPolicy": "Never", "command": ["haproxy", "-db", "-f", "/etc/haproxy/haproxy.cfg"],
                    "ports": [{"containerPort": 8443}], "resources": {"requests": {"cpu": "50m", "memory": "32Mi"},
                        "limits": {"cpu": "500m", "memory": "128Mi"}}, "securityContext": {"allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                    "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8443, "scheme": "HTTPS"}, "periodSeconds": 3},
                    "volumeMounts": [{"name": "config", "mountPath": "/etc/haproxy", "readOnly": True},
                                     {"name": "tls", "mountPath": "/run/tls", "readOnly": True}]}],
                "volumes": [{"name": "config", "configMap": {"name": name}},
                            {"name": "tls", "secret": {"secretName": "orders-entry-tls", "defaultMode": 0o440}}]}}})
    add("Service", "orders-entry", spec={"selector": {"sova.run": run, "sova.role": "edge", "sova.side": "source"},
        "ports": [{"name": "tls", "port": 8443, "targetPort": 8443}]})
    (args.output / "resources.json").write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": resources}))
    (args.output / "entry.json").write_text(json.dumps({"run_id": run, "namespace": ns,
        "certificate_sha256": cert.fingerprint(hashes.SHA256()).hex(), "expires_at": expires.isoformat(),
        "initial_side": "source", "source_ssm_port": source_port, "deployed": False,
        "automatic_request_retries": 0, "backend_identity_headers_forged": False}, indent=2) + "\n")
    print(json.dumps({"resources_rendered": len(resources), "initial_side": "source", "deployed": False}))


if __name__ == "__main__":
    main()
