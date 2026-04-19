"""
cogs/moderation.py
──────────────────
Cog de moderación completo.

Comandos slash:
  /ban         – Banear usuario
  /unban       – Desbanear por ID
  /mute        – Silenciar con rol
  /unmute      – Dessilenciar
  /kick        – Expulsar
  /warn        – Advertir (con consecuencias automáticas)
  /warns       – Ver warns de un usuario
  /clearwarns  – Limpiar warns (admin)

  /modconfig view          – Ver configuración actual
  /modconfig mute_role     – Configurar rol de mute
  /modconfig log_channel   – Configurar canal de logs
  /modconfig thresholds    – Umbrales de warns
  /modconfig consequences  – Activar/desactivar consecuencias
  /modconfig mute_duration – Duración del auto-mute
  /modconfig warn_embed    – Personalizar embed de warn (modal)
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

logger = logging.getLogger("Moderation")


# ── Utilidades de tiempo ──────────────────────────────────────────────────────

def parse_duration(raw: str) -> Optional[int]:
    """
    Convierte un string de tiempo a segundos.
    Acepta: '30s', '5m', '2h', '1d', '1w'
    Sin unidad → se interpreta como minutos.
    """
    if not raw:
        return None
    raw = raw.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    try:
        if raw[-1] in units:
            return int(raw[:-1]) * units[raw[-1]]
        return int(raw) * 60
    except (ValueError, IndexError):
        return None


def fmt_duration(seconds: Optional[int]) -> str:
    """Convierte segundos a texto legible. None → 'Permanente'."""
    if seconds is None:
        return "Permanente ♾️"
    parts = []
    for label, unit in (("sem", 604800), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= unit:
            parts.append(f"{seconds // unit}{label}")
            seconds %= unit
    return " ".join(parts) if parts else "0s"


# ── Embed de warn configurable ────────────────────────────────────────────────

def build_warn_embed(
    cfg: dict,
    usuario: discord.Member,
    moderador: discord.Member,
    razon: str,
    warns: int,
) -> discord.Embed:
    """
    Construye el embed de warn.
    Si guild_config.warn_embed_config tiene un JSON válido lo usa;
    de lo contrario aplica el embed por defecto.

    Placeholders disponibles en el JSON:
      {user}       → mención del usuario
      {username}   → nombre del usuario
      {reason}     → razón
      {warns}      → warns actuales
      {moderator}  → nombre del moderador
      {server}     → nombre del servidor
    """
    embed_cfg: Optional[dict] = None
    if cfg.get("warn_embed_config"):
        try:
            embed_cfg = json.loads(cfg["warn_embed_config"])
        except json.JSONDecodeError:
            pass

    repl = {
        "{user}": usuario.mention,
        "{username}": str(usuario),
        "{reason}": razon,
        "{warns}": str(warns),
        "{moderator}": moderador.display_name,
        "{server}": usuario.guild.name,
    }

    def sub(text: str) -> str:
        for k, v in repl.items():
            text = text.replace(k, v)
        return text

    if embed_cfg:
        raw_color = embed_cfg.get("color", "FFA500").strip("#")
        try:
            color = discord.Color(int(raw_color, 16))
        except ValueError:
            color = discord.Color.orange()

        embed = discord.Embed(
            title=sub(embed_cfg.get("title", "⚠️ Advertencia")),
            description=sub(embed_cfg.get("description", "{user} recibió una advertencia.")),
            color=color,
        )
        for field in embed_cfg.get("fields", []):
            embed.add_field(
                name=sub(field.get("name", "")),
                value=sub(field.get("value", "")),
                inline=field.get("inline", False),
            )
        if embed_cfg.get("footer"):
            embed.set_footer(text=sub(embed_cfg["footer"]))
    else:
        # Embed por defecto
        embed = discord.Embed(
            title="⚠️ Advertencia emitida",
            description=f"{usuario.mention} ha recibido una advertencia.",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👤 Usuario", value=f"{usuario.mention}\n`{usuario.id}`", inline=True)
        embed.add_field(name="👮 Moderador", value=moderador.mention, inline=True)
        embed.add_field(name="⚠️ Warns", value=f"`{warns}`", inline=True)
        embed.add_field(name="📝 Razón", value=razon, inline=False)
        embed.set_thumbnail(url=usuario.display_avatar.url)
        embed.set_footer(text=f"ID usuario: {usuario.id}")

    return embed


# ── Modal para personalizar el embed de warn ──────────────────────────────────

class WarnEmbedModal(discord.ui.Modal, title="Personalizar embed de warn"):
    emb_title = discord.ui.TextInput(
        label="Título",
        default="⚠️ Advertencia",
        max_length=256,
    )
    description = discord.ui.TextInput(
        label="Descripción  →  placeholders disponibles abajo",
        placeholder="{user} {username} {reason} {warns} {moderator} {server}",
        default="{user} recibió una advertencia en **{server}**.",
        style=discord.TextStyle.paragraph,
        max_length=1800,
    )
    color = discord.ui.TextInput(
        label="Color hex (sin #)",
        default="FFA500",
        max_length=8,
        required=False,
    )
    footer = discord.ui.TextInput(
        label="Pie de página",
        default="Moderador: {moderator}  |  Warns totales: {warns}",
        max_length=512,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction):
        cfg_json = json.dumps({
            "title": self.emb_title.value,
            "description": self.description.value,
            "color": self.color.value or "FFA500",
            "footer": self.footer.value,
            "fields": [],
        }, ensure_ascii=False)

        # Guardar en DB
        interaction.client.db.set_config(
            interaction.guild_id, warn_embed_config=cfg_json
        )

        # Vista previa
        guild_cfg = interaction.client.db.get_config(interaction.guild_id)
        preview = build_warn_embed(
            guild_cfg,
            interaction.user,   # type: ignore
            interaction.user,   # type: ignore
            "Esta es una advertencia de ejemplo",
            1,
        )
        await interaction.response.send_message(
            "✅ Embed configurado. **Vista previa:**",
            embed=preview,
            ephemeral=True,
        )


# ── Cog principal ─────────────────────────────────────────────────────────────

class Moderation(commands.Cog):
    """Comandos de moderación: ban, mute, warn, kick y configuración."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db  # type: ignore  # inyectado desde main.py
        self._check_mutes.start()

    def cog_unload(self):
        self._check_mutes.cancel()

    # ── Helpers privados ──────────────────────────────────────────────────────

    async def _send_log(self, guild: discord.Guild, embed: discord.Embed) -> None:
        cfg = self.db.get_config(guild.id)
        ch_id = cfg.get("log_channel_id")
        if not ch_id:
            return
        channel = guild.get_channel(ch_id)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                logger.warning("Sin permisos para enviar logs en %s", guild.name)

    async def _dm(self, user: discord.Member, embed: discord.Embed) -> None:
        try:
            await user.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    def _can_moderate(
        self, actor: discord.Member, target: discord.Member
    ) -> Optional[str]:
        """
        Verifica jerarquía de roles.
        Retorna None si la acción es válida, o un string de error si no.
        """
        if target.bot:
            return "No puedes moderar a un bot."
        if actor.id == target.id:
            return "No puedes moderarte a ti mismo."
        if target.id == actor.guild.owner_id:
            return "No puedes moderar al dueño del servidor."
        if actor.id != actor.guild.owner_id and actor.top_role <= target.top_role:
            return "Tu rol no es suficientemente alto para moderar a este usuario."
        bot_member = actor.guild.get_member(self.bot.user.id)
        if bot_member and bot_member.top_role <= target.top_role:
            return "Mi rol no es suficiente para moderar a este usuario."
        return None

    # ── Tarea: expiración de mutes ────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def _check_mutes(self):
        """Revisa cada minuto si algún mute temporal ha expirado."""
        try:
            for record in self.db.get_active_mutes():
                try:
                    guild = self.bot.get_guild(record["guild_id"])
                    if not guild:
                        continue

                    member = guild.get_member(record["user_id"])
                    if not member:
                        self.db.clear_mute(record["user_id"], record["guild_id"])
                        continue

                    cfg = self.db.get_config(guild.id)
                    mute_role = guild.get_role(cfg.get("mute_role_id") or 0)
                    if not mute_role or mute_role not in member.roles:
                        self.db.clear_mute(record["user_id"], guild.id)
                        continue

                    start = datetime.fromisoformat(record["mute_start"])
                    expiry = start + timedelta(seconds=record["mute_duration"])

                    if datetime.now(timezone.utc) >= expiry:
                        await member.remove_roles(
                            mute_role, reason="Mute expirado automáticamente"
                        )
                        self.db.clear_mute(record["user_id"], guild.id)
                        self.db.log_action(
                            guild.id, member.id, self.bot.user.id,
                            "AUTO_UNMUTE", "Mute temporal expirado",
                        )

                        log_embed = discord.Embed(
                            title="🔓 Mute expirado",
                            description=f"{member.mention} fue desmuteado automáticamente.",
                            color=discord.Color.green(),
                            timestamp=datetime.now(timezone.utc),
                        )
                        log_embed.set_footer(text=f"ID: {member.id}")
                        await self._send_log(guild, log_embed)
                        logger.info("Mute expirado: %s en %s", member, guild.name)

                except Exception as e:
                    logger.error("Error al expirar mute individual: %s", e)
        except Exception as e:
            logger.error("Error en _check_mutes: %s", e)

    @_check_mutes.before_loop
    async def _before_check_mutes(self):
        await self.bot.wait_until_ready()

    # ─────────────────────────────────────────────────────────────────────────
    # /ban
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="ban", description="Banea a un usuario del servidor")
    @app_commands.describe(
        usuario="Usuario a banear",
        razon="Razón del ban",
        eliminar_mensajes="Días de mensajes a eliminar (0-7, por defecto 0)",
    )
    @app_commands.checks.has_permissions(ban_members=True)
    async def ban(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        razon: str = "Sin razón especificada",
        eliminar_mensajes: app_commands.Range[int, 0, 7] = 0,
    ):
        err = self._can_moderate(interaction.user, usuario)  # type: ignore
        if err:
            return await interaction.response.send_message(f"❌ {err}", ephemeral=True)

        await interaction.response.defer()

        await self._dm(
            usuario,
            discord.Embed(
                title="🔨 Has sido baneado",
                description=f"Has sido baneado de **{interaction.guild.name}**.",
                color=discord.Color.dark_red(),
            ).add_field(name="Razón", value=razon)
             .add_field(name="Moderador", value=interaction.user.display_name),
        )

        await usuario.ban(
            reason=f"{razon} | Mod: {interaction.user}",
            delete_message_days=eliminar_mensajes,
        )
        self.db.log_action(
            interaction.guild_id, usuario.id, interaction.user.id,
            "BAN", razon, {"delete_days": eliminar_mensajes},
        )

        embed = discord.Embed(
            title="🔨 Usuario baneado",
            description=f"**{usuario}** ha sido baneado permanentemente.",
            color=discord.Color.dark_red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=usuario.display_avatar.url)
        embed.add_field(name="👤 Usuario", value=f"{usuario.mention}\n`{usuario.id}`", inline=True)
        embed.add_field(name="👮 Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="🗑️ Msgs eliminados", value=f"{eliminar_mensajes} día(s)", inline=True)
        embed.add_field(name="📝 Razón", value=razon, inline=False)

        await interaction.followup.send(embed=embed)
        await self._send_log(interaction.guild, embed)

    @ban.error
    async def ban_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await self._handle_perm_error(interaction, error)

    # ─────────────────────────────────────────────────────────────────────────
    # /unban
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="unban", description="Desbanea un usuario usando su ID")
    @app_commands.describe(
        user_id="ID numérica del usuario a desbanear",
        razon="Razón del desbaneo",
    )
    @app_commands.checks.has_permissions(ban_members=True)
    async def unban(
        self,
        interaction: discord.Interaction,
        user_id: str,
        razon: str = "Sin razón especificada",
    ):
        await interaction.response.defer()

        try:
            uid = int(user_id.strip())
        except ValueError:
            return await interaction.followup.send("❌ ID inválida.", ephemeral=True)

        try:
            entry = await interaction.guild.fetch_ban(discord.Object(id=uid))
        except discord.NotFound:
            return await interaction.followup.send(
                f"❌ No existe un ban activo con ID `{uid}`.", ephemeral=True
            )

        await interaction.guild.unban(entry.user, reason=f"{razon} | Mod: {interaction.user}")
        self.db.log_action(interaction.guild_id, uid, interaction.user.id, "UNBAN", razon)

        embed = discord.Embed(
            title="✅ Usuario desbaneado",
            description=f"**{entry.user}** fue desbaneado.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=entry.user.display_avatar.url)
        embed.add_field(name="👤 Usuario", value=f"{entry.user.mention}\n`{uid}`", inline=True)
        embed.add_field(name="👮 Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="📝 Razón", value=razon, inline=False)

        await interaction.followup.send(embed=embed)
        await self._send_log(interaction.guild, embed)

    @unban.error
    async def unban_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await self._handle_perm_error(interaction, error)

    # ─────────────────────────────────────────────────────────────────────────
    # /kick
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="kick", description="Expulsa a un usuario del servidor")
    @app_commands.describe(usuario="Usuario a expulsar", razon="Razón de la expulsión")
    @app_commands.checks.has_permissions(kick_members=True)
    async def kick(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        razon: str = "Sin razón especificada",
    ):
        err = self._can_moderate(interaction.user, usuario)  # type: ignore
        if err:
            return await interaction.response.send_message(f"❌ {err}", ephemeral=True)

        await interaction.response.defer()

        await self._dm(
            usuario,
            discord.Embed(
                title="👢 Has sido expulsado",
                description=f"Has sido expulsado de **{interaction.guild.name}**.",
                color=discord.Color.orange(),
            ).add_field(name="Razón", value=razon)
             .add_field(name="Moderador", value=interaction.user.display_name),
        )

        await usuario.kick(reason=f"{razon} | Mod: {interaction.user}")
        self.db.log_action(interaction.guild_id, usuario.id, interaction.user.id, "KICK", razon)

        embed = discord.Embed(
            title="👢 Usuario expulsado",
            description=f"**{usuario}** fue expulsado del servidor.",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=usuario.display_avatar.url)
        embed.add_field(name="👤 Usuario", value=f"{usuario.mention}\n`{usuario.id}`", inline=True)
        embed.add_field(name="👮 Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="📝 Razón", value=razon, inline=False)

        await interaction.followup.send(embed=embed)
        await self._send_log(interaction.guild, embed)

    @kick.error
    async def kick_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await self._handle_perm_error(interaction, error)

    # ─────────────────────────────────────────────────────────────────────────
    # /mute
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="mute", description="Silencia a un usuario con el rol de mute configurado")
    @app_commands.describe(
        usuario="Usuario a silenciar",
        duracion="Duración: 30m, 2h, 1d, 1w — omitir para permanente",
        razon="Razón del mute",
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def mute(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        duracion: Optional[str] = None,
        razon: str = "Sin razón especificada",
    ):
        cfg = self.db.get_config(interaction.guild_id)
        mute_role = interaction.guild.get_role(cfg.get("mute_role_id") or 0)

        if not mute_role:
            return await interaction.response.send_message(
                "❌ No hay rol de mute configurado.\n"
                "Usa `/modconfig mute_role` para asignarlo.",
                ephemeral=True,
            )

        err = self._can_moderate(interaction.user, usuario)  # type: ignore
        if err:
            return await interaction.response.send_message(f"❌ {err}", ephemeral=True)

        if mute_role in usuario.roles:
            return await interaction.response.send_message(
                f"⚠️ {usuario.mention} ya está silenciado.", ephemeral=True
            )

        secs: Optional[int] = None
        if duracion:
            secs = parse_duration(duracion)
            if secs is None:
                return await interaction.response.send_message(
                    "❌ Formato inválido. Ejemplos: `30m` · `2h` · `1d` · `1w`",
                    ephemeral=True,
                )

        await usuario.add_roles(mute_role, reason=f"Mute: {razon} | Mod: {interaction.user}")
        self.db.set_mute(usuario.id, interaction.guild_id, secs)
        self.db.log_action(
            interaction.guild_id, usuario.id, interaction.user.id,
            "MUTE", razon, {"duration_secs": secs},
        )

        embed = discord.Embed(
            title="🔇 Usuario silenciado",
            description=f"{usuario.mention} ha sido silenciado.",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👤 Usuario", value=f"{usuario.mention}\n`{usuario.id}`", inline=True)
        embed.add_field(name="👮 Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="⏱️ Duración", value=fmt_duration(secs), inline=True)
        if secs:
            expiry = datetime.now(timezone.utc) + timedelta(seconds=secs)
            embed.add_field(name="🕐 Expira", value=f"<t:{int(expiry.timestamp())}:R>", inline=True)
        embed.add_field(name="📝 Razón", value=razon, inline=False)

        await interaction.response.send_message(embed=embed)
        await self._send_log(interaction.guild, embed)

        await self._dm(
            usuario,
            discord.Embed(
                title="🔇 Has sido silenciado",
                description=f"Has sido silenciado en **{interaction.guild.name}**.",
                color=discord.Color.red(),
            ).add_field(name="Duración", value=fmt_duration(secs))
             .add_field(name="Razón", value=razon),
        )

    @mute.error
    async def mute_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await self._handle_perm_error(interaction, error)

    # ─────────────────────────────────────────────────────────────────────────
    # /unmute
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="unmute", description="Quita el silencio a un usuario")
    @app_commands.describe(usuario="Usuario a desilenciar", razon="Razón del unmute")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def unmute(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        razon: str = "Sin razón especificada",
    ):
        cfg = self.db.get_config(interaction.guild_id)
        mute_role = interaction.guild.get_role(cfg.get("mute_role_id") or 0)

        if not mute_role:
            return await interaction.response.send_message(
                "❌ No hay rol de mute configurado.", ephemeral=True
            )

        if mute_role not in usuario.roles:
            return await interaction.response.send_message(
                f"⚠️ {usuario.mention} no está silenciado.", ephemeral=True
            )

        await usuario.remove_roles(mute_role, reason=f"Unmute: {razon} | Mod: {interaction.user}")
        self.db.clear_mute(usuario.id, interaction.guild_id)
        self.db.log_action(interaction.guild_id, usuario.id, interaction.user.id, "UNMUTE", razon)

        embed = discord.Embed(
            title="🔓 Usuario desilenciado",
            description=f"{usuario.mention} fue desilenciado.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👤 Usuario", value=usuario.mention, inline=True)
        embed.add_field(name="👮 Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="📝 Razón", value=razon, inline=False)

        await interaction.response.send_message(embed=embed)
        await self._send_log(interaction.guild, embed)

        await self._dm(
            usuario,
            discord.Embed(
                title="🔓 Fuiste desilenciado",
                description=f"Tu silencio en **{interaction.guild.name}** fue levantado.",
                color=discord.Color.green(),
            ).add_field(name="Razón", value=razon),
        )

    @unmute.error
    async def unmute_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await self._handle_perm_error(interaction, error)

    # ─────────────────────────────────────────────────────────────────────────
    # /warn
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="warn", description="Advierte a un usuario (con consecuencias configurables)")
    @app_commands.describe(usuario="Usuario a advertir", razon="Razón de la advertencia")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        razon: str = "Sin razón especificada",
    ):
        err = self._can_moderate(interaction.user, usuario)  # type: ignore
        if err:
            return await interaction.response.send_message(f"❌ {err}", ephemeral=True)

        await interaction.response.defer()

        cfg = self.db.get_config(interaction.guild_id)
        warns = self.db.add_warn(usuario.id, interaction.guild_id)
        self.db.log_action(
            interaction.guild_id, usuario.id, interaction.user.id, "WARN", razon
        )

        # Embed de warn (configurable)
        warn_embed = build_warn_embed(cfg, usuario, interaction.user, razon, warns)  # type: ignore
        await interaction.followup.send(embed=warn_embed)
        await self._send_log(interaction.guild, warn_embed)
        await self._dm(usuario, warn_embed)

        # ── Consecuencias automáticas (de mayor a menor severidad) ────────────

        ban_thr   = cfg.get("warn_ban_threshold", 7)
        kick_thr  = cfg.get("warn_kick_threshold", 5)
        mute_thr  = cfg.get("warn_mute_threshold", 3)
        ban_on    = bool(cfg.get("warn_ban_enabled", 0))
        kick_on   = bool(cfg.get("warn_kick_enabled", 0))
        mute_on   = bool(cfg.get("warn_mute_enabled", 1))

        consequence_embed: Optional[discord.Embed] = None

        # Ban automático
        if ban_on and warns >= ban_thr:
            try:
                await usuario.ban(reason=f"Auto-ban: alcanzó {warns} warns")
                self.db.log_action(
                    interaction.guild_id, usuario.id, self.bot.user.id,
                    "AUTO_BAN", f"Alcanzó {warns} warns",
                )
                consequence_embed = discord.Embed(
                    title="🔨 Ban automático",
                    description=f"{usuario.mention} fue baneado por alcanzar **{warns} warns**.",
                    color=discord.Color.dark_red(),
                    timestamp=datetime.now(timezone.utc),
                )
            except discord.Forbidden:
                logger.warning("Sin permisos para auto-ban de %s", usuario)

        # Kick automático (solo si no se baneó)
        elif kick_on and warns >= kick_thr:
            try:
                await usuario.kick(reason=f"Auto-kick: alcanzó {warns} warns")
                self.db.log_action(
                    interaction.guild_id, usuario.id, self.bot.user.id,
                    "AUTO_KICK", f"Alcanzó {warns} warns",
                )
                consequence_embed = discord.Embed(
                    title="👢 Kick automático",
                    description=f"{usuario.mention} fue expulsado por alcanzar **{warns} warns**.",
                    color=discord.Color.dark_orange(),
                    timestamp=datetime.now(timezone.utc),
                )
            except discord.Forbidden:
                logger.warning("Sin permisos para auto-kick de %s", usuario)

        # Mute automático (solo si no se baneó ni kickeó)
        elif mute_on and warns >= mute_thr:
            mute_role = interaction.guild.get_role(cfg.get("mute_role_id") or 0)
            if mute_role and mute_role not in usuario.roles:
                dur = cfg.get("warn_mute_duration", 3600)
                try:
                    await usuario.add_roles(
                        mute_role, reason=f"Auto-mute: alcanzó {warns} warns"
                    )
                    self.db.set_mute(usuario.id, interaction.guild_id, dur)
                    self.db.log_action(
                        interaction.guild_id, usuario.id, self.bot.user.id,
                        "AUTO_MUTE", f"Alcanzó {warns} warns", {"duration_secs": dur},
                    )
                    consequence_embed = discord.Embed(
                        title="🔇 Mute automático",
                        description=(
                            f"{usuario.mention} fue silenciado por alcanzar **{warns} warns**.\n"
                            f"Duración: **{fmt_duration(dur)}**"
                        ),
                        color=discord.Color.red(),
                        timestamp=datetime.now(timezone.utc),
                    )
                except discord.Forbidden:
                    logger.warning("Sin permisos para auto-mute de %s", usuario)
            elif not mute_role:
                logger.warning("Auto-mute ignorado: no hay rol de mute configurado")

        if consequence_embed:
            await interaction.followup.send(embed=consequence_embed)
            await self._send_log(interaction.guild, consequence_embed)

    @warn.error
    async def warn_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await self._handle_perm_error(interaction, error)

    # ─────────────────────────────────────────────────────────────────────────
    # /warns
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="warns", description="Consulta los warns de un usuario")
    @app_commands.describe(usuario="Usuario a consultar (por defecto tú mismo)")
    async def warns_cmd(
        self,
        interaction: discord.Interaction,
        usuario: Optional[discord.Member] = None,
    ):
        target = usuario or interaction.user
        record = self.db.get_user(target.id, interaction.guild_id)  # type: ignore
        cfg = self.db.get_config(interaction.guild_id)

        w = record["warns"]
        thresholds = {
            "Mute": (cfg.get("warn_mute_threshold", 3), bool(cfg.get("warn_mute_enabled", 1))),
            "Kick": (cfg.get("warn_kick_threshold", 5), bool(cfg.get("warn_kick_enabled", 0))),
            "Ban":  (cfg.get("warn_ban_threshold", 7),  bool(cfg.get("warn_ban_enabled", 0))),
        }
        max_t = max(v[0] for v in thresholds.values())
        ratio = w / max_t if max_t else 0
        color = (
            discord.Color.green() if ratio == 0
            else discord.Color.yellow() if ratio < 0.5
            else discord.Color.orange() if ratio < 0.8
            else discord.Color.red()
        )

        embed = discord.Embed(
            title=f"📋 Warns de {target.display_name}",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="⚠️ Warns actuales", value=f"**{w}**", inline=True)

        lines = []
        for name, (thr, enabled) in thresholds.items():
            icon = "✅" if enabled else "❌"
            marker = " ← próximo" if enabled and w < thr == min(
                t for t, en in thresholds.values() if en and t > w
            ) else ""
            lines.append(f"{icon} **{name}** a los {thr} warns{marker}")

        embed.add_field(name="🎚️ Consecuencias configuradas", value="\n".join(lines), inline=False)
        embed.set_footer(text=f"ID: {target.id}")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /clearwarns
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="clearwarns", description="Limpia todos los warns de un usuario")
    @app_commands.describe(usuario="Usuario al que limpiar los warns", razon="Razón del reseteo")
    @app_commands.checks.has_permissions(administrator=True)
    async def clearwarns(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        razon: str = "Sin razón especificada",
    ):
        record = self.db.get_user(usuario.id, interaction.guild_id)
        old = record["warns"]

        if old == 0:
            return await interaction.response.send_message(
                f"ℹ️ {usuario.mention} no tiene warns.", ephemeral=True
            )

        self.db.clear_warns(usuario.id, interaction.guild_id)
        self.db.log_action(
            interaction.guild_id, usuario.id, interaction.user.id,
            "CLEAR_WARNS", razon, {"removed": old},
        )

        embed = discord.Embed(
            title="🧹 Warns limpiados",
            description=f"Se eliminaron **{old}** warn(s) de {usuario.mention}.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👤 Usuario", value=usuario.mention, inline=True)
        embed.add_field(name="👮 Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="📝 Razón", value=razon, inline=False)

        await interaction.response.send_message(embed=embed)
        await self._send_log(interaction.guild, embed)

    @clearwarns.error
    async def clearwarns_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await self._handle_perm_error(interaction, error)

    # ─────────────────────────────────────────────────────────────────────────
    # /modconfig  (grupo de subcomandos)
    # ─────────────────────────────────────────────────────────────────────────

    modconfig = app_commands.Group(
        name="modconfig",
        description="Configuración del sistema de moderación (solo administradores)",
        default_permissions=discord.Permissions(administrator=True),
    )

    @modconfig.command(name="view", description="Ver la configuración actual de moderación")
    async def mc_view(self, interaction: discord.Interaction):
        cfg = self.db.get_config(interaction.guild_id)

        mute_role = interaction.guild.get_role(cfg.get("mute_role_id") or 0)
        log_ch = interaction.guild.get_channel(cfg.get("log_channel_id") or 0)

        embed = discord.Embed(
            title="⚙️ Configuración de moderación",
            description=f"Servidor: **{interaction.guild.name}**",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="🔇 Rol de mute",
            value=mute_role.mention if mute_role else "❌ No configurado",
            inline=True,
        )
        embed.add_field(
            name="📝 Canal de logs",
            value=log_ch.mention if log_ch else "❌ No configurado",  # type: ignore
            inline=True,
        )
        embed.add_field(name="\u200b", value="\u200b", inline=False)

        cons = [
            (
                "warn_mute_enabled", "warn_mute_threshold",
                f"Mute ({fmt_duration(cfg.get('warn_mute_duration', 3600))})"
            ),
            ("warn_kick_enabled", "warn_kick_threshold", "Kick"),
            ("warn_ban_enabled",  "warn_ban_threshold",  "Ban"),
        ]
        lines = []
        for en_key, thr_key, label in cons:
            icon = "✅" if cfg.get(en_key, 0) else "❌"
            lines.append(f"{icon} **{label}** al llegar a {cfg.get(thr_key, '?')} warns")

        embed.add_field(name="🎚️ Consecuencias de warns", value="\n".join(lines), inline=False)
        embed.add_field(
            name="🖼️ Embed de warn",
            value="✅ Personalizado" if cfg.get("warn_embed_config") else "📋 Por defecto",
            inline=True,
        )
        embed.set_footer(text="Usa /modconfig <subcomando> para modificar")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @modconfig.command(name="mute_role", description="Asigna el rol que se usará para silenciar")
    @app_commands.describe(rol="Rol de mute existente en el servidor")
    async def mc_mute_role(self, interaction: discord.Interaction, rol: discord.Role):
        self.db.set_config(interaction.guild_id, mute_role_id=rol.id)
        await interaction.response.send_message(
            f"✅ Rol de mute configurado: {rol.mention}", ephemeral=True
        )

    @modconfig.command(name="log_channel", description="Canal donde se registran las acciones de moderación")
    @app_commands.describe(canal="Canal de texto para los logs")
    async def mc_log_channel(self, interaction: discord.Interaction, canal: discord.TextChannel):
        self.db.set_config(interaction.guild_id, log_channel_id=canal.id)
        await interaction.response.send_message(
            f"✅ Canal de logs: {canal.mention}", ephemeral=True
        )

    @modconfig.command(name="thresholds", description="Umbrales de warns para cada consecuencia")
    @app_commands.describe(
        mute_en="Warns necesarios para el mute automático",
        kick_en="Warns necesarios para el kick automático",
        ban_en="Warns necesarios para el ban automático",
    )
    async def mc_thresholds(
        self,
        interaction: discord.Interaction,
        mute_en: app_commands.Range[int, 1, 50] = 3,
        kick_en: app_commands.Range[int, 1, 50] = 5,
        ban_en:  app_commands.Range[int, 1, 50] = 7,
    ):
        if not (mute_en < kick_en < ban_en):
            return await interaction.response.send_message(
                "❌ Los umbrales deben ser ascendentes: mute < kick < ban.", ephemeral=True
            )
        self.db.set_config(
            interaction.guild_id,
            warn_mute_threshold=mute_en,
            warn_kick_threshold=kick_en,
            warn_ban_threshold=ban_en,
        )
        await interaction.response.send_message(
            f"✅ Umbrales actualizados:\n"
            f"• Mute → **{mute_en}** warns\n"
            f"• Kick → **{kick_en}** warns\n"
            f"• Ban  → **{ban_en}** warns",
            ephemeral=True,
        )

    @modconfig.command(
        name="consequences",
        description="Activa o desactiva las consecuencias automáticas de warns",
    )
    @app_commands.describe(
        mute="Activar mute automático",
        kick="Activar kick automático",
        ban="Activar ban automático",
    )
    async def mc_consequences(
        self,
        interaction: discord.Interaction,
        mute: Optional[bool] = None,
        kick: Optional[bool] = None,
        ban:  Optional[bool] = None,
    ):
        updates: dict = {}
        if mute is not None:
            updates["warn_mute_enabled"] = int(mute)
        if kick is not None:
            updates["warn_kick_enabled"] = int(kick)
        if ban is not None:
            updates["warn_ban_enabled"] = int(ban)

        if not updates:
            return await interaction.response.send_message(
                "❌ Especifica al menos una consecuencia.", ephemeral=True
            )

        self.db.set_config(interaction.guild_id, **updates)

        lines = []
        if mute is not None:
            lines.append(f"Mute automático: {'✅ activado' if mute else '❌ desactivado'}")
        if kick is not None:
            lines.append(f"Kick automático: {'✅ activado' if kick else '❌ desactivado'}")
        if ban is not None:
            lines.append(f"Ban automático:  {'✅ activado' if ban else '❌ desactivado'}")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @modconfig.command(name="mute_duration", description="Duración del mute automático por warns")
    @app_commands.describe(duracion="Ejemplos: 30m · 2h · 1d · 1w")
    async def mc_mute_duration(self, interaction: discord.Interaction, duracion: str):
        secs = parse_duration(duracion)
        if secs is None:
            return await interaction.response.send_message(
                "❌ Formato inválido. Ejemplos: `30m` · `2h` · `1d` · `1w`", ephemeral=True
            )
        self.db.set_config(interaction.guild_id, warn_mute_duration=secs)
        await interaction.response.send_message(
            f"✅ Duración de auto-mute: **{fmt_duration(secs)}**", ephemeral=True
        )

    @modconfig.command(name="warn_embed", description="Personaliza el embed que se envía al advertir")
    async def mc_warn_embed(self, interaction: discord.Interaction):
        await interaction.response.send_modal(WarnEmbedModal())

    # ── Manejador de errores de permisos ──────────────────────────────────────

    async def _handle_perm_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.MissingPermissions):
            msg = f"❌ Te faltan permisos: `{', '.join(error.missing_permissions)}`"
        elif isinstance(error, app_commands.BotMissingPermissions):
            msg = f"❌ Al bot le faltan permisos: `{', '.join(error.missing_permissions)}`"
        else:
            logger.error("Error en comando de moderación: %s", error, exc_info=True)
            msg = "❌ Error inesperado. Revisa los logs del bot."

        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
