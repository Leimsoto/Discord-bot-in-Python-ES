"""
api/routes/giveaways.py
───────────────────────
Endpoints de sorteos.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional
from api.deps import get_db, require_guild_admin

router = APIRouter(prefix="/api/guild/{guild_id}/giveaways", tags=["giveaways"])


@router.get("")
async def list_giveaways(
    guild_id: int,
    active_only: bool = Query(True),
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Lista sorteos del servidor."""
    giveaways = db.get_guild_giveaways(guild_id, active_only=active_only)
    return {"guild_id": guild_id, "giveaways": giveaways, "count": len(giveaways)}


@router.get("/{message_id}")
async def get_giveaway(
    guild_id: int,
    message_id: int,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Detalle de un sorteo por message_id."""
    gw = db.get_giveaway(message_id)
    if not gw or int(gw.get("guild_id", 0)) != guild_id:
        raise HTTPException(404, "Sorteo no encontrado en este servidor")
    return {"guild_id": guild_id, "giveaway": gw}
