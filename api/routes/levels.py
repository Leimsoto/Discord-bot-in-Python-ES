"""
api/routes/levels.py
────────────────────
Endpoints del sistema de niveles/XP.

GET  /api/guild/{guild_id}/levels/config         → xp_config
PUT  /api/guild/{guild_id}/levels/config         → actualizar xp_config
GET  /api/guild/{guild_id}/levels/leaderboard    → top N
GET  /api/guild/{guild_id}/levels/rewards        → recompensas
GET  /api/guild/{guild_id}/levels/user/{user_id} → datos de un usuario
"""

from fastapi import APIRouter, Depends, Query
from api.deps import get_db, require_guild_admin

router = APIRouter(prefix="/api/guild/{guild_id}/levels", tags=["levels"])


@router.get("/config")
async def get_xp_config(
    guild_id: int,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Retorna la configuración del sistema XP."""
    config = db.get_xp_config(guild_id)
    return {"guild_id": guild_id, "config": config}


@router.put("/config")
async def update_xp_config(
    guild_id: int,
    body: dict,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Actualiza la configuración del sistema XP."""
    allowed_keys = {
        "enabled", "xp_min", "xp_max", "cooldown_seconds",
        "ignored_channels", "channel_multipliers",
        "announcement_channel_id", "announcement_message", "stack_rewards",
    }
    filtered = {k: v for k, v in body.items() if k in allowed_keys}
    if filtered:
        db.set_xp_config(guild_id, **filtered)
    return {"status": "ok", "updated_keys": list(filtered.keys())}


@router.get("/leaderboard")
async def get_leaderboard(
    guild_id: int,
    limit: int = Query(10, ge=1, le=100),
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Top N del leaderboard del servidor."""
    rows = db.get_leaderboard(guild_id, limit=limit)
    return {"guild_id": guild_id, "leaderboard": rows}


@router.get("/rewards")
async def get_level_rewards(
    guild_id: int,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Lista de recompensas de nivel configuradas."""
    rewards = db.get_level_rewards(guild_id)
    return {"guild_id": guild_id, "rewards": rewards}


@router.get("/user/{user_id}")
async def get_user_level(
    guild_id: int,
    user_id: int,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Datos de nivel/XP de un usuario específico."""
    data = db.get_user_level(user_id, guild_id)
    rank = db.get_user_rank(user_id, guild_id)

    return {
        "guild_id": guild_id,
        "user_id": user_id,
        "data": data,
        "rank": rank,
    }
