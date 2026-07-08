"""
api/routes/autoroles.py
───────────────────────
Endpoints para Autoroles.

Dos modos:
  • Join autoroles: roles que se asignan al entrar al servidor.
  • Reaction roles: paneles donde el usuario reacciona para obtener un rol.

Endpoints:
  GET    /api/guilds/{guild_id}/autoroles/join              → Lista de roles join
  POST   /api/guilds/{guild_id}/autoroles/join              → Agrega rol join
  DELETE /api/guilds/{guild_id}/autoroles/join/{role_id}    → Quita rol join

  GET    /api/guilds/{guild_id}/autoroles/reactions                  → Paneles
  POST   /api/guilds/{guild_id}/autoroles/reactions                  → Crear/actualizar panel
  DELETE /api/guilds/{guild_id}/autoroles/reactions/{message_id}     → Eliminar panel
"""

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import get_db, require_guild_admin
from api.snowflakes import coerce_snowflake, serialize_snowflake, stringify_rows

router = APIRouter(
    prefix="/api/guilds/{guild_id}/autoroles",
    tags=["autoroles"],
)


def _normalize_mapping_data(raw: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400,
            detail="El campo mapping_data debe ser un JSON válido.",
        )
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="mapping_data debe ser un objeto JSON.")
    normalized = {str(emoji): coerce_snowflake(role_id, "role_id") for emoji, role_id in data.items()}
    return json.dumps(normalized, ensure_ascii=False)


def _panel_payload(row: dict) -> dict:
    out = stringify_rows([row], {"guild_id", "channel_id", "message_id"})[0]
    try:
        mapping = json.loads(out.get("mapping_data") or "{}")
        if isinstance(mapping, dict):
            out["mapping_data"] = json.dumps({str(k): serialize_snowflake(v) for k, v in mapping.items()}, ensure_ascii=False)
    except Exception:
        pass
    return out


# ── Join Autoroles ───────────────────────────────────────────────────────────

class JoinRoleBody(BaseModel):
    role_id: int | str = Field(..., description="ID del rol a asignar al unirse")


@router.get("/join")
async def list_join_roles(
    guild_id: int,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Lista los roles configurados para asignación al unirse."""
    rows = db.get_join_autoroles(guild_id)
    return {"guild_id": serialize_snowflake(guild_id), "join_roles": stringify_rows(rows, {"role_id", "guild_id"})}


@router.post("/join")
async def add_join_role(
    guild_id: int,
    body: JoinRoleBody,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Agrega un rol a la lista de auto-asignación."""
    role_id = coerce_snowflake(body.role_id, "role_id")
    db.add_join_autorole(guild_id, role_id)
    return {"status": "ok", "role_id": serialize_snowflake(role_id)}


@router.delete("/join/{role_id}")
async def remove_join_role(
    guild_id: int,
    role_id: int,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Quita un rol de la lista de auto-asignación."""
    db.remove_join_autorole(guild_id, role_id)
    return {"status": "ok", "role_id": serialize_snowflake(role_id)}


# ── Reaction Roles ───────────────────────────────────────────────────────────

class ReactionPanelBody(BaseModel):
    message_id: int | str
    channel_id: int | str
    mapping_data: str = Field(..., description="JSON: {emoji: role_id}")


@router.get("/reactions")
async def list_reaction_panels(
    guild_id: int,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Lista todos los paneles de reaction-role configurados."""
    panels = db.get_guild_autoroles(guild_id)
    return {"guild_id": serialize_snowflake(guild_id), "panels": [_panel_payload(p) for p in panels]}


@router.post("/reactions")
async def upsert_reaction_panel(
    guild_id: int,
    body: ReactionPanelBody,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Crea o actualiza un panel de reaction-role."""
    mapping_data = _normalize_mapping_data(body.mapping_data)

    message_id = coerce_snowflake(body.message_id, "message_id")
    channel_id = coerce_snowflake(body.channel_id, "channel_id")
    db.set_autorole(
        message_id=message_id,
        guild_id=guild_id,
        channel_id=channel_id,
        mapping_data=mapping_data,
    )
    return {"status": "ok", "message_id": serialize_snowflake(message_id), "channel_id": serialize_snowflake(channel_id)}


@router.delete("/reactions/{message_id}")
async def delete_reaction_panel(
    guild_id: int,
    message_id: int,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Elimina un panel de reaction-role."""
    panel = db.get_autorole(message_id)
    if not panel or int(panel.get("guild_id", 0)) != guild_id:
        raise HTTPException(status_code=404, detail="Panel no encontrado en este servidor.")
    db.delete_autorole(message_id)
    return {"status": "ok", "message_id": serialize_snowflake(message_id)}
