"""
api/routes/autoroles.py
───────────────────────
Endpoints para configuración de autoroles y reaction roles.

GET    /api/guild/{guild_id}/autoroles             → Lista de autoroles
POST   /api/guild/{guild_id}/autoroles             → Crear/Actualizar autorol (mapping_data)
DELETE /api/guild/{guild_id}/autoroles/{msg_id}    → Eliminar autorol
"""

from fastapi import APIRouter, Depends, HTTPException
from api.deps import get_db, require_guild_admin
from pydantic import BaseModel
import json

router = APIRouter(prefix="/api/guild/{guild_id}/autoroles", tags=["autoroles"])

class AutoroleCreate(BaseModel):
    message_id: int
    channel_id: int
    mapping_data: str

@router.get("")
async def get_autoroles(
    guild_id: int,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Lista todos los paneles de autorol configurados para el servidor."""
    autoroles = db.get_guild_autoroles(guild_id)
    return {
        "guild_id": guild_id,
        "autoroles": autoroles
    }

@router.post("")
async def create_or_update_autorole(
    guild_id: int,
    body: AutoroleCreate,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Crea o actualiza la configuración de autorol/reacción de un mensaje."""
    # Validate JSON mapping data
    try:
        json.loads(body.mapping_data)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="El campo mapping_data debe ser un JSON válido.")

    db.set_autorole(
        message_id=body.message_id,
        guild_id=guild_id,
        channel_id=body.channel_id,
        mapping_data=body.mapping_data
    )
    return {"status": "ok", "message": "Autorol actualizado correctamente."}

@router.delete("/{message_id}")
async def delete_autorole(
    guild_id: int,
    message_id: int,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Elimina la configuración de autorol de un mensaje."""
    # Verify the autorole belongs to the guild
    autorole = db.get_autorole(message_id)
    if not autorole or autorole.get("guild_id") != guild_id:
        raise HTTPException(status_code=404, detail="Autorol no encontrado en este servidor.")

    db.delete_autorole(message_id)
    return {"status": "ok", "message": "Autorol eliminado."}
