"""
api/auth.py
───────────
Autenticación Discord OAuth2.
Endpoints listos para conectar con el panel web (React/Next.js).

Flujo OAuth2:
  1. Frontend redirige a /api/auth/login
  2. Discord redirige a /api/auth/callback con el code
  3. Backend intercambia code por access_token
  4. Backend obtiene datos del usuario y sus guilds
  5. Genera un JWT y lo devuelve al frontend
  6. Frontend usa el JWT en todas las peticiones subsiguientes

Variables de entorno necesarias (.env):
  DISCORD_CLIENT_ID      — ID de la aplicación
  DISCORD_CLIENT_SECRET  — Secret de la aplicación
  JWT_SECRET             — Clave para firmar los JWT
  DASHBOARD_URL          — URL del frontend (para redirect)
"""

import os
import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import RedirectResponse

logger = logging.getLogger("API.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])

DISCORD_API = "https://discord.com/api/v10"
DISCORD_CDN = "https://cdn.discordapp.com"


def _get_oauth_config() -> dict:
    """Lee la config de OAuth2 desde el entorno."""
    return {
        "client_id": os.getenv("DISCORD_CLIENT_ID", ""),
        "client_secret": os.getenv("DISCORD_CLIENT_SECRET", ""),
        "jwt_secret": os.getenv("JWT_SECRET", ""),
        "dashboard_url": os.getenv("DASHBOARD_URL", "http://localhost:3000"),
        "redirect_uri": os.getenv("DASHBOARD_URL", "http://localhost:3000").rstrip("/")
                        + "/api/auth/callback",
    }


@router.get("/login")
async def login():
    """
    Redirige al usuario a la página de autorización de Discord.
    El frontend debe dirigir al usuario aquí para iniciar sesión.
    """
    cfg = _get_oauth_config()
    if not cfg["client_id"]:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OAuth2 no configurado. Define DISCORD_CLIENT_ID en .env",
        )

    params = (
        f"client_id={cfg['client_id']}"
        f"&redirect_uri={cfg['redirect_uri']}"
        f"&response_type=code"
        f"&scope=identify+guilds"
    )
    return RedirectResponse(
        url=f"https://discord.com/oauth2/authorize?{params}",
    )


@router.get("/callback")
async def callback(code: str):
    """
    Callback de Discord OAuth2.
    Intercambia el code por un access_token, obtiene datos del usuario
    y genera un JWT para el frontend.
    """
    import httpx

    cfg = _get_oauth_config()
    if not cfg["client_id"] or not cfg["client_secret"] or not cfg["jwt_secret"]:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OAuth2 no completamente configurado en .env",
        )

    # Intercambiar code por access_token
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": cfg["redirect_uri"],
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if token_resp.status_code != 200:
        logger.warning(f"Error en OAuth2 token exchange: {token_resp.text}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Error al autenticar con Discord",
        )

    token_data = token_resp.json()
    access_token = token_data["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}

    # Obtener datos del usuario y sus guilds
    async with httpx.AsyncClient() as client:
        user_resp = await client.get(f"{DISCORD_API}/users/@me", headers=headers)
        guilds_resp = await client.get(f"{DISCORD_API}/users/@me/guilds", headers=headers)

    if user_resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No se pudieron obtener los datos del usuario",
        )

    user_data = user_resp.json()
    guilds_data = guilds_resp.json() if guilds_resp.status_code == 200 else []

    # Filtrar solo guilds donde el usuario es admin o owner
    admin_guilds = [
        {"id": g["id"], "name": g["name"], "icon": g.get("icon"),
         "permissions": g.get("permissions", 0), "owner": g.get("owner", False)}
        for g in guilds_data
        if g.get("owner") or (int(g.get("permissions", 0)) & 0x8)
    ]

    # Generar JWT
    import jwt
    payload = {
        "sub": str(user_data["id"]),
        "username": user_data.get("username", ""),
        "discriminator": user_data.get("discriminator", "0"),
        "avatar": user_data.get("avatar"),
        "guilds": admin_guilds,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(hours=24),
    }
    token = jwt.encode(payload, cfg["jwt_secret"], algorithm="HS256")

    # Redirigir al frontend con el token
    dashboard = cfg["dashboard_url"].rstrip("/")
    return RedirectResponse(url=f"{dashboard}/auth/callback?token={token}")


@router.get("/me")
async def get_me():
    """
    Devuelve los datos del usuario autenticado.
    El frontend debe enviar el JWT en el header Authorization: Bearer <token>.

    (Este endpoint será funcional cuando se configure JWT_SECRET)
    """
    from api.deps import get_current_user
    # Nota: en producción esto usa el dependency injection de FastAPI.
    # Aquí es un endpoint informativo que indica el formato esperado.
    return {
        "info": "Envía el JWT como header Authorization: Bearer <token>",
        "ejemplo_respuesta": {
            "user_id": 123456789,
            "username": "usuario",
            "avatar_url": f"{DISCORD_CDN}/avatars/123456789/abc123.webp",
            "admin_guilds": [
                {"id": "987654321", "name": "Mi Servidor", "icon": "..."}
            ],
        },
    }
