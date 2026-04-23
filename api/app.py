"""
api/app.py
──────────
Aplicación FastAPI principal.
Reemplaza el antiguo mantener_vivo.py (Flask keep-alive)
y prepara todos los endpoints REST para el panel web.

Funcionalidades:
  • Health-check en `/` (mantiene compatibilidad con UptimeRobot)
  • CORS configurado para desarrollo y producción
  • Inyección del DatabaseManager compartido vía app.state
  • Router modular por recurso
"""

import logging
import os
from threading import Thread

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger("API")


def create_app(db=None) -> FastAPI:
    """
    Factoría de la aplicación FastAPI.
    Recibe opcionalmente la instancia de DatabaseManager
    para compartirla con el bot de Discord.
    """
    app = FastAPI(
        title="TortuguBot API",
        description="Backend REST para el panel web del bot de Discord",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    dashboard_url = os.getenv("DASHBOARD_URL", "http://localhost:3000")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            dashboard_url,
            "http://localhost:3000",
            "http://localhost:5173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Inyectar DB ───────────────────────────────────────────────────────────
    if db is not None:
        app.state.db = db

    # ── Registrar routers ─────────────────────────────────────────────────────
    from api.routes import (
        guild,
        moderation,
        tickets,
        tags,
        levels,
        reports,
        schedules,
        giveaways,
        autoroles,
        radio,
        embeds,
        channels,
    )

    app.include_router(guild.router)
    app.include_router(moderation.router)
    app.include_router(tickets.router)
    app.include_router(tags.router)
    app.include_router(levels.router)
    app.include_router(reports.router)
    app.include_router(schedules.router)
    app.include_router(giveaways.router)
    app.include_router(autoroles.router)
    app.include_router(radio.router)
    app.include_router(embeds.router)
    app.include_router(channels.router)

    # ── Registrar auth ────────────────────────────────────────────────────────
    from api.auth import router as auth_router
    app.include_router(auth_router)

    # ── Health-check (reemplaza al home() de Flask) ───────────────────────────
    @app.get("/", tags=["health"])
    async def health():
        return {"status": "online", "bot": "TortuguBot 🐢", "api": "v1.0.0"}

    @app.get("/api/health", tags=["health"])
    async def api_health():
        return {"status": "ok"}

    return app


def iniciar_api(db=None, host: str = "0.0.0.0", port: int = 8080) -> None:
    """
    Arranca el servidor FastAPI en un hilo daemon.
    Diseñado para ser llamado desde main.py antes de bot.run(),
    exactamente como hacía mantener_vivo() con Flask.
    """
    import uvicorn

    app = create_app(db=db)

    def _run():
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level="warning",  # Silenciar logs de uvicorn para no saturar la consola
            access_log=False,
        )

    t = Thread(target=_run, daemon=True)
    t.start()
    logger.info(f"API FastAPI iniciada en http://{host}:{port}")
