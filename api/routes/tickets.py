"""
api/routes/tickets.py
─────────────────────
Endpoints del sistema de tickets.

GET  /api/guild/{guild_id}/tickets/config      → config + categorías
PUT  /api/guild/{guild_id}/tickets/config      → actualizar config
GET  /api/guild/{guild_id}/tickets             → lista (paginado, filtro)
GET  /api/guild/{guild_id}/tickets/{ticket_id} → detalle
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException, status
from api.deps import get_db, require_guild_admin

router = APIRouter(prefix="/api/guild/{guild_id}/tickets", tags=["tickets"])


@router.get("/config")
async def get_ticket_config(
    guild_id: int,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Retorna la configuración de tickets y sus categorías."""
    config = db.get_ticket_config(guild_id)
    categories = db.get_ticket_categories(guild_id)
    return {
        "guild_id": guild_id,
        "config": config,
        "categories": categories,
    }


@router.put("/config")
async def update_ticket_config(
    guild_id: int,
    body: dict,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Actualiza la configuración de tickets."""
    allowed_keys = {
        "panel_channel_id", "category_id", "log_channel_id",
        "allowed_roles", "immune_roles", "panel_embed_data",
        "channel_name_template", "max_tickets_per_user",
        "ticket_cooldown_seconds",
    }
    filtered = {k: v for k, v in body.items() if k in allowed_keys}
    if filtered:
        db.set_ticket_config(guild_id, **filtered)
    return {"status": "ok", "updated_keys": list(filtered.keys())}


@router.get("")
async def list_tickets(
    guild_id: int,
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Lista de tickets del servidor (paginado, con filtro de estado)."""
    tickets = db.get_all_tickets(guild_id, status=status_filter, limit=limit, offset=offset)
    total_open = db.count_open_tickets_by_guild(guild_id)
    return {
        "guild_id": guild_id,
        "tickets": tickets,
        "open_count": total_open,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{ticket_id}")
async def get_ticket_detail(
    guild_id: int,
    ticket_id: int,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Detalle de un ticket específico."""
    ticket = db.get_ticket(ticket_id)
    if not ticket or int(ticket.get("guild_id", 0)) != guild_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ticket #{ticket_id} no encontrado en este servidor",
        )
    return {"guild_id": guild_id, "ticket": ticket}
