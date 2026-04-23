"""
api/routes/reports.py
─────────────────────
Endpoints del sistema de reportes.

GET  /api/guild/{guild_id}/reports          → lista (filtro estado)
GET  /api/guild/{guild_id}/reports/{id}     → detalle
PUT  /api/guild/{guild_id}/reports/{id}     → actualizar estado
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException, status
from api.deps import get_db, require_guild_admin

router = APIRouter(prefix="/api/guild/{guild_id}/reports", tags=["reports"])


@router.get("")
async def list_reports(
    guild_id: int,
    status_filter: Optional[str] = Query(None, alias="status"),
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Lista reportes del servidor con filtro opcional por estado."""
    reports = db.get_reports(guild_id, status=status_filter)
    return {"guild_id": guild_id, "reports": reports, "count": len(reports)}


@router.get("/{report_id}")
async def get_report(
    guild_id: int,
    report_id: int,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Detalle de un reporte específico."""
    reports = db.get_reports(guild_id)
    report = next((r for r in reports if int(r["id"]) == report_id), None)
    if not report:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Reporte #{report_id} no encontrado en este servidor",
        )
    return {"guild_id": guild_id, "report": report}


@router.put("/{report_id}")
async def update_report(
    guild_id: int,
    report_id: int,
    body: dict,
    db=Depends(get_db),
    _user=Depends(require_guild_admin),
):
    """Actualiza el estado de un reporte (RESOLVED, DISMISSED, etc)."""
    # Verificar que el reporte existe y pertenece al guild
    reports = db.get_reports(guild_id)
    report = next((r for r in reports if int(r["id"]) == report_id), None)
    if not report:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Reporte #{report_id} no encontrado en este servidor",
        )

    allowed_keys = {"status", "ticket_id"}
    filtered = {k: v for k, v in body.items() if k in allowed_keys}
    if not filtered:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Se requiere al menos 'status' o 'ticket_id'",
        )

    db.update_report(report_id, **filtered)
    return {"status": "ok", "report_id": report_id, "updated": filtered}
