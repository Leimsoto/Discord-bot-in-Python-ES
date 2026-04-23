"""
Paquete `api`.
Backend FastAPI para el panel web del bot.
Exporta `iniciar_api()` para arrancarlo en un hilo daemon desde main.py.
"""
from .app import iniciar_api

__all__ = ["iniciar_api"]
