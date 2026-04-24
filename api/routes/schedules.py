"""
api/routes/schedules.py
───────────────────────
Endpoints de mensajes programados.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from api.deps import get_db, require_guild_admin

router = APIRouter(prefix="/api/guild/{guild_id}/schedules", tags=["schedules"])

MAX_SCHEDULES = 10
MIN_INTERVAL = 600
MAX_INTERVAL = 2_592_000


@router.get("")
async def list_schedules(guild_id: int, db=Depends(get_db), _user=Depends(require_guild_admin)):
    schedules = db.get_schedules(guild_id)
    return {"guild_id": guild_id, "schedules": schedules, "count": len(schedules)}


@router.post("", status_code=201)
async def create_schedule(guild_id: int, body: dict, db=Depends(get_db), user=Depends(require_guild_admin)):
    name = body.get("name", "").strip()
    channel_id = body.get("channel_id")
    content = body.get("content", "").strip()
    interval_seconds = body.get("interval_seconds", 0)

    if not name or not channel_id or not content:
        raise HTTPException(400, "Se requieren 'name', 'channel_id' y 'content'")
    if interval_seconds < MIN_INTERVAL:
        raise HTTPException(400, f"Intervalo mínimo: {MIN_INTERVAL}s (10 min)")
    if interval_seconds > MAX_INTERVAL:
        raise HTTPException(400, f"Intervalo máximo: {MAX_INTERVAL}s (30 días)")

    existing = db.get_schedules(guild_id)
    if len(existing) >= MAX_SCHEDULES:
        raise HTTPException(400, f"Máximo de {MAX_SCHEDULES} schedules alcanzado")
    if any(s["name"] == name for s in existing):
        raise HTTPException(409, f"Ya existe un schedule llamado '{name}'")

    creator_id = user.get("user_id", 0)
    db.create_schedule(guild_id, name, int(channel_id), content, int(interval_seconds), creator_id)
    return {"status": "created", "name": name}


@router.put("/{schedule_id}")
async def update_schedule(guild_id: int, schedule_id: int, body: dict, db=Depends(get_db), _user=Depends(require_guild_admin)):
    # Verificar que el schedule pertenece a este guild
    schedules = db.get_schedules(guild_id)
    sched = next((s for s in schedules if int(s["id"]) == schedule_id), None)
    if not sched:
        raise HTTPException(404, f"Schedule #{schedule_id} no encontrado en este servidor")

    allowed = {"enabled", "channel_id", "content", "interval_seconds", "last_sent"}
    filtered = {k: v for k, v in body.items() if k in allowed}
    if not filtered:
        raise HTTPException(400, "No se proporcionaron campos válidos")
    db.update_schedule(schedule_id, **filtered)
    return {"status": "ok", "schedule_id": schedule_id, "updated": filtered}


@router.delete("/{name}")
async def delete_schedule(guild_id: int, name: str, db=Depends(get_db), _user=Depends(require_guild_admin)):
    sched = db.get_schedule_by_name(guild_id, name)
    if not sched:
        raise HTTPException(404, f"Schedule '{name}' no encontrado")
    db.delete_schedule(guild_id, name)
    return {"status": "deleted", "name": name}
