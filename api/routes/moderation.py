"""
api/routes/moderation.py
────────────────────────
Endpoints de moderación.

GET  /api/guild/{guild_id}/moderation/actions        → historial (paginado)
GET  /api/guild/{guild_id}/moderation/user/{user_id}  → historial de un usuario
GET  /api/guild/{guild_id}/moderation/warns           → usuarios con warns activos
"""

from fastapi import APIRouter, Depends, Query
from api.deps import get_db, require_guild_admin

router = APIRouter(prefix="/api/guild/{guild_id}/moderation", tags=["moderation"])


@router.get("/actions")
async def get_mod_actions(
    guild_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Lista de acciones de moderación del servidor (paginado)."""
    actions = db.get_mod_actions(guild_id, limit=limit, offset=offset)
    return {"guild_id": guild_id, "actions": actions, "limit": limit, "offset": offset}


@router.get("/user/{user_id}")
async def get_user_mod_history(
    guild_id: int,
    user_id: int,
    limit: int = Query(20, ge=1, le=100),
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Historial de moderación de un usuario específico."""
    history = db.get_user_history(user_id, guild_id, limit=limit)
    summary = db.get_user_action_summary(user_id, guild_id)
    user_record = db.get_user(user_id, guild_id)

    return {
        "guild_id": guild_id,
        "user_id": user_id,
        "user_record": user_record,
        "summary": summary,
        "history": history,
    }


@router.get("/warns")
async def get_active_warns(
    guild_id: int,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Lista de usuarios con warns activos en el servidor."""
    users = db.get_users_with_warns(guild_id)
    return {"guild_id": guild_id, "users_with_warns": users}
