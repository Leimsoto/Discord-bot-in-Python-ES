"""
mantener_vivo.py
────────────────
Pequeño servidor Flask que responde al ping de UptimeRobot u otros
servicios, evitando que la instancia se duerma en Replit / Render free tier.
"""

from threading import Thread
from flask import Flask

_app = Flask(__name__)


@_app.route("/")
def home():
    return "🐢 Bot activo"


def mantener_vivo():
    t = Thread(target=lambda: _app.run(host="0.0.0.0", port=8080), daemon=True)
    t.start()
