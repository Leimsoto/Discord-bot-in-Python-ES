"""
cogs/scheduler.py
─────────────────
Sistema de mensajes programados (cron).

Comandos:
  /schedule create   — Abre modal: nombre, canal, mensaje, intervalo
  /schedule list     — Lista schedules del servidor
  /schedule delete   — Elimina un schedule
  /schedule toggle   — Activa/desactiva
  /schedule test     — Envía el mensaje una vez ahora

Restricciones:
  - Solo administradores
  - Intervalo mínimo: 10 minutos
  - Máximo 10 schedules por servidor
"""

import logging
import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

logger = logging.getLogger(__name__)

MIN_INTERVAL = 600       # 10 minutos en segundos
MAX_INTERVAL = 2_592_000 # 30 días en segundos
MAX_SCHEDULES = 10


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_interval(text: str) -> int:
    """Convierte '1h', '30m', '2d', '1w' a segundos. Devuelve -1 si inválido."""
    text = text.strip().lower()
    units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    if len(text) < 2:
        return -1
    unit = text[-1]
    if unit not in units:
        return -1
    try:
        n = float(text[:-1])
        return int(n * units[unit])
    except ValueError:
        return -1


def _fmt_interval(seconds: int) -> str:
    """Formatea segundos como '2h 30m'."""
    parts = []
    for unit, label in [(604800, "sem"), (86400, "d"), (3600, "h"), (60, "min")]:
        if seconds >= unit:
            parts.append(f"{seconds // unit}{label}")
            seconds %= unit
    return " ".join(parts) if parts else f"{seconds}s"


async def _schedule_autocomplete(interaction: discord.Interaction, current: str):
    schedules = interaction.client.db.get_schedules(interaction.guild_id)
    return [
        app_commands.Choice(name=s["name"], value=s["name"])
        for s in schedules if current.lower() in s["name"].lower()
    ][:25]


# ── Modal ─────────────────────────────────────────────────────────────────────

class ScheduleCreateModal(discord.ui.Modal, title="Crear Mensaje Programado"):
    name_input = discord.ui.TextInput(
        label="Nombre identificador",
        placeholder="recordatorio-semanal",
        max_length=50,
        min_length=1,
    )
    content_input = discord.ui.TextInput(
        label="Mensaje a enviar",
        style=discord.TextStyle.paragraph,
        placeholder="@everyone ¡Recuerden...",
        max_length=2000,
        min_length=1,
    )
    interval_input = discord.ui.TextInput(
        label="Intervalo (ej: 30m, 1h, 2d, 1w)",
        placeholder="1h",
        max_length=10,
    )

    def __init__(self, cog, channel: discord.TextChannel):
        super().__init__()
        self.cog = cog
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        name = self.name_input.value.strip()
        content = self.content_input.value.strip()
        interval_str = self.interval_input.value.strip()

        interval_secs = _parse_interval(interval_str)
        if interval_secs < MIN_INTERVAL:
            return await interaction.response.send_message(
                f"❌ El intervalo mínimo es **10 minutos** (`10m`). Ingresaste: `{interval_str}`.", ephemeral=True
            )
        if interval_secs > MAX_INTERVAL:
            return await interaction.response.send_message(
                f"❌ El intervalo máximo es **30 días** (`30d`).", ephemeral=True
            )

        existing = self.cog.db.get_schedules(interaction.guild_id)
        if len(existing) >= MAX_SCHEDULES:
            return await interaction.response.send_message(
                f"❌ Ya tienes el máximo de {MAX_SCHEDULES} mensajes programados.", ephemeral=True
            )
        if any(s["name"] == name for s in existing):
            return await interaction.response.send_message(
                f"❌ Ya existe un schedule llamado **{name}**.", ephemeral=True
            )

        self.cog.db.create_schedule(
            interaction.guild_id, name, self.channel.id,
            content, interval_secs, interaction.user.id
        )
        await interaction.response.send_message(
            f"✅ Mensaje programado **{name}** creado.\n"
            f"📍 Canal: {self.channel.mention} | ⏱️ Intervalo: `{_fmt_interval(interval_secs)}`",
            ephemeral=True,
        )


# ── Cog ───────────────────────────────────────────────────────────────────────

class Scheduler(commands.Cog):
    """Mensajes programados periódicos (tipo cron)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db  # type: ignore
        self.cron_runner.start()

    def cog_unload(self):
        self.cron_runner.cancel()

    @tasks.loop(seconds=60)
    async def cron_runner(self):
        now = datetime.now(timezone.utc)
        try:
            schedules = self.db.get_all_active_schedules()
        except Exception as e:
            logger.warning(f"Error leyendo schedules: {e}")
            return

        for sched in schedules:
            try:
                interval = int(sched["interval_seconds"])
                last_sent_str = sched.get("last_sent")

                if last_sent_str:
                    last_sent = datetime.fromisoformat(last_sent_str)
                    elapsed = (now - last_sent).total_seconds()
                    if elapsed < interval:
                        continue

                guild = self.bot.get_guild(int(sched["guild_id"]))
                if not guild:
                    continue
                channel = guild.get_channel(int(sched["channel_id"]))
                if not channel or not isinstance(channel, discord.TextChannel):
                    continue

                await channel.send(sched["content"])
                self.db.update_schedule(int(sched["id"]), last_sent=now.isoformat())
                logger.info(f"Schedule '{sched['name']}' enviado en {guild.name}#{channel.name}")
            except Exception as e:
                logger.warning(f"Error en schedule '{sched.get('name', '?')}': {e}")

    @cron_runner.before_loop
    async def before_cron(self):
        await self.bot.wait_until_ready()

    # ── Comandos ──────────────────────────────────────────────────────────────

    schedule_group = app_commands.Group(
        name="schedule",
        description="Mensajes programados periódicos",
        default_member_permissions=discord.Permissions(administrator=True),
    )

    @schedule_group.command(name="create", description="Crea un mensaje programado que se enviará automáticamente")
    @app_commands.describe(canal="Canal donde se enviará el mensaje")
    @app_commands.checks.has_permissions(administrator=True)
    async def schedule_create(self, interaction: discord.Interaction, canal: discord.TextChannel):
        await interaction.response.send_modal(ScheduleCreateModal(self, canal))

    @schedule_group.command(name="list", description="Lista todos los mensajes programados del servidor")
    @app_commands.checks.has_permissions(administrator=True)
    async def schedule_list(self, interaction: discord.Interaction):
        schedules = self.db.get_schedules(interaction.guild_id)
        if not schedules:
            return await interaction.response.send_message(
                "📭 No hay mensajes programados. Crea uno con `/schedule create`.", ephemeral=True
            )

        embed = discord.Embed(
            title="⏱️ Mensajes Programados",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        for s in schedules:
            ch = interaction.guild.get_channel(int(s["channel_id"]))
            ch_text = ch.mention if ch else f"ID:{s['channel_id']}"
            status = "🟢" if s["enabled"] else "🔴"
            last = s["last_sent"][:16].replace("T", " ") if s["last_sent"] else "Nunca"
            embed.add_field(
                name=f"{status} {s['name']}",
                value=f"Canal: {ch_text}\nIntervalo: `{_fmt_interval(int(s['interval_seconds']))}`\nÚltimo envío: `{last}`",
                inline=True,
            )
        embed.set_footer(text=f"{len(schedules)}/{MAX_SCHEDULES} schedules")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @schedule_group.command(name="delete", description="Elimina un mensaje programado")
    @app_commands.describe(nombre="Nombre del schedule a eliminar")
    @app_commands.autocomplete(nombre=_schedule_autocomplete)
    @app_commands.checks.has_permissions(administrator=True)
    async def schedule_delete(self, interaction: discord.Interaction, nombre: str):
        sched = self.db.get_schedule_by_name(interaction.guild_id, nombre)
        if not sched:
            return await interaction.response.send_message(f"❌ No existe ningún schedule llamado **{nombre}**.", ephemeral=True)
        self.db.delete_schedule(interaction.guild_id, nombre)
        await interaction.response.send_message(f"✅ Schedule **{nombre}** eliminado.", ephemeral=True)

    @schedule_group.command(name="toggle", description="Activa o desactiva un mensaje programado")
    @app_commands.describe(nombre="Nombre del schedule")
    @app_commands.autocomplete(nombre=_schedule_autocomplete)
    @app_commands.checks.has_permissions(administrator=True)
    async def schedule_toggle(self, interaction: discord.Interaction, nombre: str):
        sched = self.db.get_schedule_by_name(interaction.guild_id, nombre)
        if not sched:
            return await interaction.response.send_message(f"❌ No existe ningún schedule llamado **{nombre}**.", ephemeral=True)
        new_state = 0 if sched["enabled"] else 1
        self.db.update_schedule(int(sched["id"]), enabled=new_state)
        state_text = "✅ Activado" if new_state else "🔴 Desactivado"
        await interaction.response.send_message(f"{state_text} — Schedule **{nombre}**.", ephemeral=True)

    @schedule_group.command(name="test", description="Envía el mensaje programado una vez ahora mismo")
    @app_commands.describe(nombre="Nombre del schedule")
    @app_commands.autocomplete(nombre=_schedule_autocomplete)
    @app_commands.checks.has_permissions(administrator=True)
    async def schedule_test(self, interaction: discord.Interaction, nombre: str):
        sched = self.db.get_schedule_by_name(interaction.guild_id, nombre)
        if not sched:
            return await interaction.response.send_message(f"❌ No existe ningún schedule llamado **{nombre}**.", ephemeral=True)
        channel = interaction.guild.get_channel(int(sched["channel_id"]))
        if not channel or not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("❌ Canal no encontrado.", ephemeral=True)
        try:
            await channel.send(sched["content"])
            await interaction.response.send_message(f"✅ Mensaje de prueba enviado a {channel.mention}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Sin permisos para enviar en ese canal.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Scheduler(bot))
