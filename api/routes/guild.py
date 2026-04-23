"""
api/routes/guild.py
───────────────────
Endpoints de configuración general del servidor.

GET  /api/guild/{guild_id}/config   → guild_config + server_config combinados
PUT  /api/guild/{guild_id}/config   → actualizar configuración
GET  /api/guild/{guild_id}/stats    → estadísticas del bot
"""

from fastapi import APIRouter, Depends
from api.deps import get_db, require_guild_admin

router = APIRouter(prefix="/api/guild/{guild_id}", tags=["guild"])


@router.get("/config")
async def get_guild_config(
    guild_id: int,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Retorna la configuración completa del servidor."""
    guild_cfg = db.get_config(guild_id)
    server_cfg = db.get_server_config(guild_id)
    ai_cfg = db.get_ai_config(guild_id)
    welcome_cfg = db.get_welcome_config(guild_id)
    boost_cfg = db.get_boost_config(guild_id)
    suggestions_cfg = db.get_suggestions_config(guild_id)

    return {
        "guild_id": guild_id,
        "guild_config": guild_cfg,
        "server_config": server_cfg,
        "ai_config": ai_cfg,
        "welcome_config": welcome_cfg,
        "boost_config": boost_cfg,
        "suggestions_config": suggestions_cfg,
    }


@router.put("/config")
async def update_guild_config(
    guild_id: int,
    body: dict,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """
    Actualiza la configuración del servidor.
    El body puede contener una o más secciones:
      { "guild_config": {...}, "server_config": {...}, "ai_config": {...} }
    """
    updated = []

    if "guild_config" in body:
        db.set_config(guild_id, **body["guild_config"])
        updated.append("guild_config")

    if "server_config" in body:
        db.set_server_config(guild_id, **body["server_config"])
        updated.append("server_config")

    if "ai_config" in body:
        db.set_ai_config(guild_id, **body["ai_config"])
        updated.append("ai_config")

    if "welcome_config" in body:
        db.set_welcome_config(guild_id, **body["welcome_config"])
        updated.append("welcome_config")

    if "boost_config" in body:
        db.set_boost_config(guild_id, **body["boost_config"])
        updated.append("boost_config")

    if "suggestions_config" in body:
        db.set_suggestions_config(guild_id, **body["suggestions_config"])
        updated.append("suggestions_config")

    return {"status": "ok", "updated": updated}


@router.get("/stats")
async def get_guild_stats(
    guild_id: int,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Retorna estadísticas del bot para este servidor."""
    bot_stats = db.get_bot_stats()
    open_tickets = db.count_open_tickets_by_guild(guild_id)

    return {
        "guild_id": guild_id,
        "bot_stats": bot_stats,
        "open_tickets_guild": open_tickets,
    }
