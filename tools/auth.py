import os
import secrets

from fastapi import Header, HTTPException, Request, status

from tools.db import TenantConfig, get_tenant_by_api_key


def require_admin(x_admin_key: str = Header(...)) -> None:
    admin_key = os.getenv("ADMIN_KEY", "").strip()
    if not admin_key:
        raise HTTPException(status_code=503, detail="Admin endpoint not configured")
    if not secrets.compare_digest(x_admin_key, admin_key):
        raise HTTPException(status_code=401, detail="Invalid admin key")


def _resolve_tenant(api_key: str) -> TenantConfig:
    tenant = get_tenant_by_api_key(api_key)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    if not tenant.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account suspended. Contact support.",
        )
    return tenant


def require_tenant(x_api_key: str = Header(...)) -> TenantConfig:
    return _resolve_tenant(x_api_key)


def require_tenant_dashboard(
    request: Request,
    api_key: str = "",
    x_api_key: str = Header(default=""),
) -> TenantConfig:
    key = api_key or x_api_key
    if not key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    return _resolve_tenant(key)
