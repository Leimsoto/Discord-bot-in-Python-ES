"""
api/routes/channels.py
──────────────────────
Endpoints para configuraciones específicas de canales (ej. ignorar comandos, XP, logs).

GET /api/guild/{guild_id}/channels              → Configs especiales de todos los canales
PUT /api/guild/{guild_id}/channels/{channel_id} → Actualizar la config de un canal
"""

from fastapi import APIRouter, Depends
from api.deps import get_db, require_guild_admin
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/api/guild/{guild_id}/channels", tags=["channels"])

class ChannelConfigUpdate(BaseModel):
    ignore_commands: Optional[int] = None
    ignore_xp: Optional[int] = None
    is_log_channel: Optional[int] = None
    log_types: Optional[str] = None
    auto_publish: Optional[int] = None
    auto_thread: Optional[int] = None
    thread_name: Optional[str] = None

@router.get("")
async def get_all_channel_configs(
    guild_id: int,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Obtiene una lista de todos los canales que tienen configuración especial guardada."""
    channels = db.get_all_channel_configs(guild_id)
    return {
        "guild_id": guild_id,
        "channels": channels
    }

@router.put("/{channel_id}")
async def update_channel_config(
    guild_id: int,
    channel_id: int,
    body: ChannelConfigUpdate,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Actualiza o crea las opciones de un canal específico (ignorar xp, etc)."""
    update_data = {k: v for k, v in body.model_dump().items() if v is not None}
    
    if update_data:
        db.set_channel_config(channel_id, guild_id, **update_data)
        
    return {"status": "ok", "message": "Configuración de canal actualizada."}
