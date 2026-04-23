"""
api/routes/radio.py
───────────────────
Endpoints para configuración de la radio Lofi 24/7.

GET /api/guild/{guild_id}/radio/config   → Obtiene config de la radio
PUT /api/guild/{guild_id}/radio/config   → Actualiza config de la radio
"""

from fastapi import APIRouter, Depends
from api.deps import get_db, require_guild_admin
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/api/guild/{guild_id}/radio", tags=["radio"])

class RadioConfigUpdate(BaseModel):
    enabled: Optional[int] = None
    channel_id: Optional[int] = None
    stream_url: Optional[str] = None
    station_name: Optional[str] = None
    volume: Optional[int] = None

@router.get("/config")
async def get_radio_config(
    guild_id: int,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Obtiene la configuración actual de la radio 24/7."""
    cfg = db.get_lofi_config(guild_id)
    return {
        "guild_id": guild_id,
        "radio_config": cfg
    }

@router.put("/config")
async def update_radio_config(
    guild_id: int,
    body: RadioConfigUpdate,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Actualiza la configuración de la radio (canal, stream, estado, volumen)."""
    update_data = {k: v for k, v in body.model_dump().items() if v is not None}
    
    if update_data:
        db.set_lofi_config(guild_id, **update_data)
        
    return {"status": "ok", "message": "Configuración de radio actualizada."}
