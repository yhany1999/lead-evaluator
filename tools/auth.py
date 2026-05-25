from fastapi import Header, HTTPException, status

from tools.db import TenantConfig, get_tenant_by_api_key


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
