"""
api/routes/embeds.py
────────────────────
Endpoints para configuración y gestión de Embeds Personalizados guardados.

GET    /api/guild/{guild_id}/embeds          → Lista embeds
GET    /api/guild/{guild_id}/embeds/{name}   → Detalle de embed
POST   /api/guild/{guild_id}/embeds          → Guarda embed
DELETE /api/guild/{guild_id}/embeds/{id}     → Elimina embed
"""

from fastapi import APIRouter, Depends, HTTPException
from api.deps import get_db, require_guild_admin
from pydantic import BaseModel
import json

router = APIRouter(prefix="/api/guild/{guild_id}/embeds", tags=["embeds"])

class EmbedCreate(BaseModel):
    name: str
    embed_data: str

@router.get("")
async def list_embeds(
    guild_id: int,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Obtiene la lista de plantillas de embeds guardadas en el servidor."""
    embeds = db.get_saved_embeds(guild_id)
    return {
        "guild_id": guild_id,
        "embeds": embeds
    }

@router.get("/{name}")
async def get_embed(
    guild_id: int,
    name: str,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Obtiene el JSON completo de un embed por su nombre."""
    embed = db.get_saved_embed_by_name(guild_id, name)
    if not embed:
        raise HTTPException(status_code=404, detail="Embed no encontrado.")
        
    return embed

@router.post("")
async def create_embed(
    guild_id: int,
    body: EmbedCreate,
    db=Depends(get_db),
    user=Depends(require_guild_admin),
):
    """Guarda una nueva plantilla de embed personalizado."""
    # Validar que sea un JSON string correcto
    try:
        json.loads(body.embed_data)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="El campo embed_data debe ser un JSON válido.")

    # La función original en database/manager.py toma 4 argumentos
    # Def: add_saved_embed(self, guild_id: int, creator_id: int, name: str, embed_data: str)
    try:
        creator_id = int(user.get("id")) if user and user.get("id") else 0
        
        # Check if already exists to delete and replace or we can just try adding it
        existing = db.get_saved_embed_by_name(guild_id, body.name)
        if existing:
            db.delete_saved_embed(existing["id"])
            
        db._execute(
            "INSERT INTO saved_embeds (guild_id, creator_id, name, embed_data, created_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (guild_id, creator_id, body.name, body.embed_data)
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al guardar: {str(e)}")

    return {"status": "ok", "message": f"Embed '{body.name}' guardado correctamente."}

@router.delete("/{embed_id}")
async def delete_embed(
    guild_id: int,
    embed_id: int,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Elimina una plantilla de embed por su ID."""
    db.delete_saved_embed(embed_id)
    return {"status": "ok", "message": "Embed eliminado."}
