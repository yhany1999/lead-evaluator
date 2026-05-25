import os
import secrets

from fastapi import Header, HTTPException, status

from tools.db import TenantConfig, get_tenant_by_api_key


def require_admin(x_admin_key: str = Header(...)) -> None:
    admin_key = os.getenv("ADMIN_KEY", "").strip()
    if not admin_key:
        raise HTTPException(status_code=503, detail="Admin endpoint not configured")
    if not secrets.compare_digest(x_admin_key, admin_key):
        raise HTTPException(status_code=401, detail="Invalid admin key")


def require_tenant(x_api_key: str = Header(...)) -> TenantConfig:
    tenant = get_tenant_by_api_key(x_api_key)
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
