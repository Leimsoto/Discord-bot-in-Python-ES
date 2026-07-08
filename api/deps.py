"""
api/deps.py
───────────
Dependencias compartidas:
  • get_db()                     — Inyecta DatabaseManager
  • get_current_user()           — JWT desde Bearer header o cookie
  • get_current_user_from_request() — Para uso directo con Request
  • require_guild_admin()        — Verifica admin/owner del guild
"""

import logging
import os
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger("API.deps")
_bearer_scheme = HTTPBearer(auto_error=False)


def _forbidden_detail(
    *,
    message: str,
    guild_id: int,
    live_status: dict,
    jwt_has_guild: bool,
) -> dict:
    return {
        "message": message,
        "guild_id": str(guild_id),
        "reason": live_status.get("reason"),
        "jwt_has_guild": jwt_has_guild,
        "bot_available": live_status.get("bot_available"),
        "guild_available": live_status.get("guild_available"),
        "member_available": live_status.get("member_available"),
        "is_owner": live_status.get("is_owner"),
        "administrator": live_status.get("administrator"),
        "manage_guild": live_status.get("manage_guild"),
    }


def _log_forbidden(request: Request, user: dict, detail: dict) -> None:
    path = getattr(getattr(request, "url", None), "path", "<unknown>")
    logger.warning(
        "403 dashboard permission path=%s user=%s guild=%s reason=%s jwt_has_guild=%s "
        "bot=%s guild_available=%s member=%s owner=%s admin=%s manage_guild=%s",
        path,
        user.get("user_id"),
        detail.get("guild_id"),
        detail.get("reason"),
        detail.get("jwt_has_guild"),
        detail.get("bot_available"),
        detail.get("guild_available"),
        detail.get("member_available"),
        detail.get("is_owner"),
        detail.get("administrator"),
        detail.get("manage_guild"),
    )


def get_db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(503, "Base de datos no disponible")
    return db


def get_bot(request: Request):
    """Inyecta la instancia del bot de Discord (puede ser None en tests)."""
    return getattr(request.app.state, "bot", None)


def _decode_jwt(token: str) -> dict:
    """Decodifica un JWT y devuelve el payload."""
    import jwt as pyjwt

    jwt_secret = os.getenv("JWT_SECRET", "")
    if not jwt_secret:
        raise HTTPException(503, "JWT_SECRET no configurado")
    try:
        payload = pyjwt.decode(token, jwt_secret, algorithms=["HS256"])
        return {
            "user_id": int(payload["sub"]),
            "username": payload.get("username", ""),
            "avatar": payload.get("avatar", ""),
            "guilds": payload.get("guilds", []),
            "is_dev_mode": False,
        }
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(
            401, "Token expirado", headers={"WWW-Authenticate": "Bearer"}
        )
    except Exception as e:
        logger.warning(f"Token inválido: {e}")
        raise HTTPException(
            401, "Token inválido", headers={"WWW-Authenticate": "Bearer"}
        )


async def get_current_user_from_request(request: Request) -> dict:
    """Extrae y valida el usuario desde Authorization header o cookie."""
    jwt_secret = os.getenv("JWT_SECRET", "")
    master_key = os.getenv("MASTER_ADMIN_KEY", "")

    # Sin seguridad configurada → modo dev
    if not jwt_secret and not master_key:
        logger.debug("Sin JWT_SECRET — modo desarrollo")
        return {"user_id": 0, "username": "dev", "is_dev_mode": True, "guilds": []}

    # Buscar token: 1) Authorization: Bearer, 2) Cookie botES_token
    token: Optional[str] = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    elif "botES_token" in request.cookies:
        token = request.cookies["botES_token"]

    if not token:
        raise HTTPException(
            401,
            "Token de autenticación requerido",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Master Key bypass
    if master_key and token == master_key:
        return {
            "user_id": 0,
            "username": "MasterAdmin",
            "guilds": [],
            "is_dev_mode": False,
            "is_master_admin": True,
        }

    return _decode_jwt(token)


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> dict:
    return await get_current_user_from_request(request)


async def require_guild_admin(
    guild_id: int,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    if user.get("is_dev_mode") or user.get("is_master_admin"):
        return user

    user_guilds = user.get("guilds", [])
    match = next((g for g in user_guilds if int(g.get("id", 0)) == guild_id), None)
    if match:
        perms = int(match.get("permissions", 0))
        if bool(perms & 0x8) or bool(perms & 0x20) or match.get("owner"):
            return user

    live_status = await get_guild_admin_status(request, guild_id, user)
    if live_status.get("allowed"):
        # El JWT puede estar viejo después de cambiar dominio/callback o roles.
        # Si el bot conectado confirma permisos reales, permitir la operación.
        user["is_live_guild_admin"] = True
        return user

    if not match:
        detail = _forbidden_detail(
            message="No tienes acceso a este servidor",
            guild_id=guild_id,
            live_status=live_status,
            jwt_has_guild=False,
        )
        _log_forbidden(request, user, detail)
        raise HTTPException(403, detail)

    detail = _forbidden_detail(
        message="Necesitas ser administrador o dueño del servidor",
        guild_id=guild_id,
        live_status=live_status,
        jwt_has_guild=True,
    )
    _log_forbidden(request, user, detail)
    raise HTTPException(403, detail)


async def _has_live_guild_admin(request: Request, guild_id: int, user: dict) -> bool:
    status = await get_guild_admin_status(request, guild_id, user)
    return bool(status.get("allowed"))


async def get_guild_admin_status(request: Request, guild_id: int, user: dict) -> dict:
    """Describe permisos reales en el guild usando el bot conectado.

    El OAuth/JWT guarda un snapshot de permisos. En producción ese snapshot puede
    quedar desactualizado tras cambiar dominio, callback, roles o volver a invitar
    el bot. Para evitar 403 falsos en el dashboard, las mutaciones aceptan también
    la autoridad observada por Discord en vivo: owner, Administrator o Manage Guild.
    """
    base = {
        "allowed": False,
        "source": "live_bot",
        "bot_available": False,
        "guild_available": False,
        "member_available": False,
        "is_owner": False,
        "administrator": False,
        "manage_guild": False,
        "reason": None,
    }
    bot = getattr(request.app.state, "bot", None)
    user_id = user.get("user_id")
    if bot is None or not user_id:
        base["reason"] = "bot_or_user_unavailable"
        return base
    base["bot_available"] = True

    try:
        user_id_int = int(user_id)
        guild = bot.get_guild(int(guild_id))
    except Exception:
        base["reason"] = "invalid_user_or_guild_id"
        return base

    if guild is None:
        base["reason"] = "guild_not_found_in_bot"
        return base
    base["guild_available"] = True
    if user_id_int == guild.owner_id:
        base.update({"allowed": True, "member_available": True, "is_owner": True})
        return base

    member = guild.get_member(user_id_int)
    if member is None:
        try:
            member = await guild.fetch_member(user_id_int)
        except Exception as exc:
            logger.debug(
                "No se pudo verificar permisos live para user=%s guild=%s: %s",
                user_id_int,
                guild_id,
                exc,
            )
            base["reason"] = "member_not_found_or_fetch_failed"
            return base
    base["member_available"] = True

    perms = getattr(member, "guild_permissions", None)
    if perms:
        base["administrator"] = bool(perms.administrator)
        base["manage_guild"] = bool(perms.manage_guild)
        base["allowed"] = bool(perms.administrator or perms.manage_guild)
    if not base["allowed"]:
        base["reason"] = "member_lacks_admin_or_manage_guild"
    return base


async def require_master_admin(user: dict = Depends(get_current_user)) -> dict:
    """
    Restringe a operaciones globales del bot (gestión del pool de API keys, etc).
    En dev mode (sin JWT_SECRET configurado) se permite. En prod sólo si el
    token coincide con MASTER_ADMIN_KEY.
    """
    if user.get("is_dev_mode") or user.get("is_master_admin"):
        return user
    raise HTTPException(403, "Operación restringida al administrador del bot")
