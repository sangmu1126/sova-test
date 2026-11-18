"""Optional same-origin OIDC workspace. Public assets never grant API rights."""

from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, Response, RedirectResponse

from sova.domain.contracts import ContractError, identifier
from sova.interfaces.web_assets import ConsoleWebBundle


def _url(value, *, origin=False):
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(c.isspace() for c in value)
        or (parsed.scheme == "http" and parsed.hostname != "127.0.0.1")
        or (origin and parsed.path)
        or value.endswith("/")
    ):
        raise ContractError("Explicit HTTPS URL or local loopback URL required")
    return parsed


def mount_workspace(app, *, root, origin, issuer, client_id, tenant_id, audience):
    origin_url, issuer_url = _url(origin, origin=True), _url(issuer)
    bundle = ConsoleWebBundle(root)
    workspace = Path(root) / "workspace/index.html"
    if (
        not workspace.is_file()
        or workspace.is_symlink()
        or workspace.parent.is_symlink()
        or workspace.stat().st_size > 131072
    ):
        raise ContractError("Bounded compiled workspace entry required")
    html = workspace.read_bytes()
    config = {
        "version": 1,
        "issuer": issuer,
        "client_id": identifier(client_id),
        "tenant_id": identifier(tenant_id),
        "audience": identifier(audience),
        "origin": origin,
        "redirect_uri": origin + "/workspace/callback",
        "post_logout_redirect_uri": origin + "/workspace/",
    }
    issuer_origin = f"{issuer_url.scheme}://{issuer_url.netloc}"
    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Content-Security-Policy": f"default-src 'self'; script-src 'self'; style-src 'self'; "
        f"connect-src 'self' {issuer_origin}; img-src 'self' data:; font-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'",
    }

    def boundary(request):
        if request.headers.getlist("host") != [origin_url.netloc]:
            raise ContractError("Workspace origin mismatch")

    @app.get("/workspace/config")
    def workspace_config(request: Request):
        boundary(request)
        return JSONResponse(config, headers=headers)

    @app.get("/workspace/")
    @app.get("/workspace/callback")
    def workspace_entry(request: Request):
        boundary(request)
        return Response(html, media_type="text/html", headers=headers)

    @app.get("/console/assets/{name}")
    def workspace_asset(name: str, request: Request):
        boundary(request)
        value = bundle.get("/console/assets/" + name)
        if value is None:
            return JSONResponse(
                {"error": "not found"}, status_code=404, headers=headers
            )
        data, kind = value
        return Response(data, media_type=kind, headers=headers)

    @app.get("/")
    def workspace_root(request: Request):
        boundary(request)
        return RedirectResponse("/workspace/", status_code=307, headers=headers)

    return config
