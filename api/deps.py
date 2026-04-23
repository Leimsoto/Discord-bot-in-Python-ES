"""
api/deps.py
───────────
Dependencias compartidas para los endpoints:
  • get_db()  — Inyecta la instancia de DatabaseManager
  • get_current_user() — Extrae y valida el usuario del token JWT (placeholder)
  • require_guild_admin() — Verifica que el usuario es admin/owner del guild
"""

import os
import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

logger = logging.getLogger("API.deps")

# Esquema de seguridad Bearer para Swagger UI
_bearer_scheme = HTTPBearer(auto_error=False)


def get_db(request: Request):
    """
    Inyecta la instancia de DatabaseManager almacenada en app.state.
    Todos los endpoints la reciben vía Depends(get_db).
    """
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Base de datos no disponible",
        )
    return db


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> dict:
    """
    Extrae el usuario autenticado del token JWT.

    Cuando el panel web esté conectado, este dependency:
      1. Lee el token Bearer del header Authorization
      2. Lo decodifica con PyJWT
      3. Devuelve un dict con {user_id, username, guilds, ...}

    Por ahora retorna un placeholder que permite probar los endpoints
    sin autenticación configurada. Cuando configures OAuth2, descomentar
    la validación real.
    """
    jwt_secret = os.getenv("JWT_SECRET")

    # ── Sin JWT_SECRET configurado → modo desarrollo (sin auth) ───────────
    if not jwt_secret:
        logger.debug("JWT_SECRET no configurado — modo desarrollo (sin auth)")
        return {"user_id": 0, "username": "dev", "is_dev_mode": True}

    # ── Con JWT_SECRET → validar token ────────────────────────────────────
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token de autenticación requerido",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        import jwt
        payload = jwt.decode(
            credentials.credentials,
            jwt_secret,
            algorithms=["HS256"],
        )
        return {
            "user_id": int(payload["sub"]),
            "username": payload.get("username", ""),
            "guilds": payload.get("guilds", []),
            "is_dev_mode": False,
        }
    except Exception as e:
        logger.warning(f"Token JWT inválido: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido o expirado",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_guild_admin(
    guild_id: int,
    user: dict = Depends(get_current_user),
) -> dict:
    """
    Verifica que el usuario autenticado tiene permisos de admin/owner
    en el guild solicitado.

    En modo desarrollo (sin JWT_SECRET), deja pasar todo.
    En producción, verifica que el guild_id está en la lista de guilds
    del usuario con permisos de administrador.
    """
    if user.get("is_dev_mode"):
        return user

    user_guilds = user.get("guilds", [])
    # Buscar el guild en los guilds del usuario
    # Cada guild en el token tiene: {id, permissions, owner}
    guild_match = next(
        (g for g in user_guilds if int(g.get("id", 0)) == guild_id),
        None,
    )

    if not guild_match:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes acceso a este servidor",
        )

    # Verificar admin: bit 0x8 (ADMINISTRATOR) en permissions, o es owner
    permissions = int(guild_match.get("permissions", 0))
    is_admin = bool(permissions & 0x8)
    is_owner = guild_match.get("owner", False)

    if not (is_admin or is_owner):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Necesitas ser administrador o dueño del servidor",
        )

    return user
