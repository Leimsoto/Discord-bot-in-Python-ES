"""
cogs/moderation.py
──────────────────
Cog de moderación completo.

Comandos slash:
  /ban         – Banear usuario
  /tempban     – Banear temporalmente
  /unban       – Desbanear por ID
  /mute        – Silenciar con rol
  /unmute      – Dessilenciar
  /kick        – Expulsar
  /warn        – Advertir (con consecuencias automáticas)
  /warns       – Ver warns de un usuario
  /clearwarns  – Limpiar warns (admin)
  /appeals list – Listar apelaciones

  La configuración se gestiona desde el Dashboard Web.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

def _voice(cog):
    """Return bot.catbot_voice if available, else a no-op fallback."""
    v = getattr(cog.bot, "catbot_voice", None)
    if v:
        return v
    class _Fallback:
        def line(self, role, text): return text
        def get(self, name): return ""
        def embed(self, title=None, description=None, kind="info", url=None):
            import discord
            return discord.Embed(title=title, description=description)
    return _Fallback()



logger = logging.getLogger("Moderation")

URL_RE = re.compile(r"(?:https?://|www\.)", re.IGNORECASE)
INVITE_RE = re.compile(r"discord\.gg/|discord(?:app)?\.com/invite/", re.IGNORECASE)


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
            if not isinstance(embed_cfg, dict):
                logger.warning("warn_embed_config inválido: no es un objeto JSON")
                embed_cfg = None
        except json.JSONDecodeError:
            logger.warning("warn_embed_config contiene JSON inválido")

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
        embed.add_field(name="Usuario", value=f"{usuario.mention}\n`{usuario.id}`", inline=True)
        embed.add_field(name="Moderador", value=moderador.mention, inline=True)
        embed.add_field(name="⚠️ Warns", value=f"`{warns}`", inline=True)
        embed.add_field(name="Razón", value=razon, inline=False)
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
        self._check_tempbans.start()
        self._check_pending_moderation.start()
        self._flush_log_outbox.start()

    def cog_unload(self):
        self._check_mutes.cancel()
        self._check_tempbans.cancel()
        self._check_pending_moderation.cancel()
        self._flush_log_outbox.cancel()

    # ── Helpers privados ──────────────────────────────────────────────────────

    async def _resolve_modlog(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        """Resuelve el canal de modlog. Usa caché y, si falla, intenta fetch.

        Devuelve ``None`` si el modlog está deshabilitado, sin configurar o
        si el canal ya no existe / el bot no lo ve.
        """
        srv_cfg = self.db.get_server_config(guild.id)
        if not srv_cfg.get("modlog_enabled", 1):
            return None

        ch_id = srv_cfg.get("modlog_channel")
        if not ch_id:
            return None

        channel = guild.get_channel(ch_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(ch_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None

        if not isinstance(channel, discord.TextChannel):
            logger.warning(
                "Canal de modlog inválido o no accesible en %s (%s)",
                guild.name, ch_id,
            )
            return None
        return channel

    async def _send_log(self, guild: discord.Guild, embed: discord.Embed) -> bool:
        """Envía un embed al canal de modlog. Devuelve True si tuvo éxito."""
        channel = await self._resolve_modlog(guild)
        if channel is None:
            self._enqueue_modlog_outbox(guild, embed, None, "modlog_channel_unavailable")
            return False
        try:
            await channel.send(embed=embed)
            return True
        except discord.Forbidden:
            logger.warning("Sin permisos para enviar logs en %s", guild.name)
            self._enqueue_modlog_outbox(guild, embed, channel.id, "missing_send_messages")
        except discord.HTTPException as exc:
            logger.warning("No se pudo enviar modlog en %s: %s", guild.name, exc)
            self._enqueue_modlog_outbox(guild, embed, channel.id, str(exc))
        return False

    def _enqueue_modlog_outbox(
        self,
        guild: discord.Guild,
        embed: discord.Embed,
        channel_id: Optional[int],
        error: Optional[str] = None,
    ) -> None:
        """Guarda un log crítico para reintento si el envío directo falla."""
        try:
            srv_cfg = self.db.get_server_config(guild.id)
            if not srv_cfg.get("modlog_enabled", 1):
                return
            target_channel_id = channel_id or srv_cfg.get("modlog_channel")
            if not target_channel_id:
                return
            self.db.enqueue_log_outbox(
                guild.id,
                "moderation",
                {
                    "embed": embed.to_dict(),
                    "source": "moderation_cog",
                    "last_error": (error or "")[:500],
                },
                channel_id=int(target_channel_id),
            )
        except Exception as exc:
            logger.warning("No se pudo encolar modlog fallido en %s: %s", guild.name, exc)

    async def _notify_modlog_issue(self, interaction: discord.Interaction) -> None:
        """Avisa al moderador (ephemeral followup) que el modlog falló.

        Discrimina entre: no configurado, canal inválido y sin permisos.
        """
        srv_cfg = self.db.get_server_config(interaction.guild_id)
        if not srv_cfg.get("modlog_enabled", 1):
            return
        ch_id = srv_cfg.get("modlog_channel")
        if not ch_id:
            msg = (
                "⚠️ La acción se aplicó pero **no hay canal de mod-logs**."
                " Configúralo en el dashboard → Moderación."
            )
        else:
            msg = (
                f"⚠️ La acción se aplicó pero el canal de mod-logs <#{ch_id}>"
                " no es accesible (borrado o sin permisos del bot)."
                " Revisa la configuración en el dashboard."
            )
        try:
            await interaction.followup.send(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    async def _safe_interaction_send(
        self,
        interaction: discord.Interaction,
        content: Optional[str] = None,
        *,
        embed: Optional[discord.Embed] = None,
        ephemeral: bool = False,
    ) -> bool:
        """Responde una interacción sin dejar que tokens expirados rompan el comando."""
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)
            else:
                await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
            return True
        except discord.NotFound:
            logger.warning("No se pudo responder: interacción/webhook expirado o desconocido")
            return False
        except discord.HTTPException as exc:
            logger.warning("No se pudo responder interacción: %s", exc)
            return False

    async def _safe_defer(self, interaction: discord.Interaction, *, ephemeral: bool = False) -> bool:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(thinking=True, ephemeral=ephemeral)
            return True
        except discord.NotFound:
            logger.warning("No se pudo deferir: interacción expirada o desconocida")
            return False
        except discord.HTTPException as exc:
            logger.warning("No se pudo deferir interacción: %s", exc)
            return False

    async def _ensure_mute_role(
        self, guild: discord.Guild,
    ) -> Optional[discord.Role]:
        """Devuelve el rol de mute. Si no existe, intenta crear uno.

        Reglas:
          1. Si ``config.mute_role_id`` apunta a un rol existente → devolverlo.
          2. Si hay un rol llamado "Muted"/"Silenciado" → adoptarlo y persistir id.
          3. Si el bot tiene ``manage_roles`` y ``manage_channels`` → crear rol
             "Muted" + deny ``send_messages`` y ``speak`` en cada canal.
        """
        cfg = self.db.get_config(guild.id)
        try:
            role_id = int(cfg.get("mute_role_id") or 0)
        except (TypeError, ValueError):
            role_id = 0
        role = guild.get_role(role_id) if role_id else None
        if role:
            return role

        for name in ("Muted", "Silenciado", "Muteado"):
            existing = discord.utils.find(
                lambda r: r.name.lower() == name.lower(), guild.roles
            )
            if existing:
                try:
                    self.db.set_config(guild.id, mute_role_id=existing.id)
                except Exception as exc:
                    logger.warning("No se pudo persistir mute_role_id: %s", exc)
                await self._apply_mute_overwrites(guild, existing)
                return existing

        bot_member = guild.get_member(self.bot.user.id) if self.bot.user else None
        if not bot_member or not bot_member.guild_permissions.manage_roles:
            return None
        try:
            new_role = await guild.create_role(
                name="Muted",
                reason="Auto-creado para auto-mute por warns",
                color=discord.Color.dark_grey(),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("No se pudo crear rol Muted en %s: %s", guild.name, exc)
            return None

        # Aplicar overrides en canales (best-effort).
        await self._apply_mute_overwrites(guild, new_role)

        try:
            self.db.set_config(guild.id, mute_role_id=new_role.id)
        except Exception as exc:
            logger.warning("No se pudo persistir mute_role_id nuevo: %s", exc)
        return new_role

    async def _dm(self, user: discord.Member, embed: discord.Embed, view: Optional[discord.ui.View] = None) -> None:
        try:
            if view:
                await user.send(embed=embed, view=view)
            else:
                await user.send(embed=embed)
        except discord.Forbidden:
            logger.info("No se pudo enviar DM a %s (%s): DMs cerrados o bloqueados", user, user.id)
        except discord.HTTPException as exc:
            logger.warning("Error enviando DM a %s (%s): %s", user, user.id, exc)

    def _has_mod_perms(self, interaction: discord.Interaction, perm_name: str) -> bool:
        user = interaction.user
        if not interaction.guild_id or not isinstance(user, discord.Member):
            return False

        if getattr(user.guild_permissions, "administrator", False):
            return True
        if getattr(user.guild_permissions, perm_name, False):
            return True

        srv = self.db.get_server_config(interaction.guild_id)
        r_ids = [r.id for r in user.roles]
        if srv.get("mod_role_id") in r_ids or srv.get("staff_role_id") in r_ids:
            return True
        return False

    def _has_config_perms(self, interaction: discord.Interaction) -> bool:
        """Permisos para tocar configuración del bot en el servidor."""
        user = interaction.user
        guild = interaction.guild
        if not guild or not isinstance(user, discord.Member):
            return False
        if user.id == guild.owner_id:
            return True
        perms = user.guild_permissions
        return bool(perms.administrator or perms.manage_guild)

    async def _apply_mute_overwrites(self, guild: discord.Guild, role: discord.Role) -> int:
        """Aplica permisos del rol Muted en canales visibles para el bot, best-effort."""
        bot_member = guild.get_member(self.bot.user.id) if self.bot.user else None
        if not bot_member or not bot_member.guild_permissions.manage_channels:
            return 0
        overwrite = discord.PermissionOverwrite(
            send_messages=False,
            add_reactions=False,
            speak=False,
            send_messages_in_threads=False,
            create_public_threads=False,
            create_private_threads=False,
        )
        changed = 0
        for ch in guild.channels:
            try:
                await ch.set_permissions(role, overwrite=overwrite, reason="Mute role setup")
                changed += 1
            except (discord.Forbidden, discord.HTTPException):
                continue
        return changed

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
        if target.guild_permissions.administrator and actor.id != actor.guild.owner_id:
            return "No puedes moderar a un administrador protegido."
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

                    if self.db.has_pending_moderation_action(
                        guild.id,
                        record["user_id"],
                        ["MUTE_EXPIRE", "HARDMUTE_EXPIRE", "UNMUTE"],
                    ):
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

                    try:
                        start = datetime.fromisoformat(record["mute_start"])
                    except (TypeError, ValueError):
                        logger.warning(
                            "Registro de mute inválido para user_id=%s guild_id=%s",
                            record.get("user_id"),
                            record.get("guild_id"),
                        )
                        self.db.clear_mute(record["user_id"], guild.id)
                        continue

                    expiry = start + timedelta(seconds=record["mute_duration"])

                    if datetime.now(timezone.utc) >= expiry:
                        try:
                            await member.remove_roles(
                                mute_role, reason="Mute expirado automáticamente"
                            )
                        except discord.Forbidden:
                            logger.warning("Sin permisos para quitar mute a %s en %s", member, guild.name)
                            continue
                        except discord.HTTPException as exc:
                            logger.warning("Error quitando mute expirado a %s en %s: %s", member, guild.name, exc)
                            continue

                        self.db.clear_mute(record["user_id"], guild.id)
                        self.db.log_action(
                            guild.id, member.id, self.bot.user.id,
                            "AUTO_UNMUTE", "Mute temporal expirado",
                        )

                        log_embed = discord.Embed(
                            title="Mute expirado",
                            description=f"{member.mention} fue desmuteado automáticamente.",
                            color=discord.Color.green(),
                            timestamp=datetime.now(timezone.utc),
                        )
                        log_embed.set_footer(text=f"ID: {member.id}")
                        await self._send_log(guild, log_embed)
                        logger.info("Mute expirado: %s en %s", member, guild.name)

                except Exception as exc:
                    logger.error("Error al expirar mute individual: %s", exc, exc_info=True)
        except Exception as exc:
            logger.error("Error en _check_mutes: %s", exc, exc_info=True)

    @_check_mutes.before_loop
    async def _before_check_mutes(self):
        await self.bot.wait_until_ready()

    # ── Tarea: expiración de tempbans ──────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def _check_tempbans(self):
        """Revisa cada minuto si algún tempban ha expirado."""
        try:
            for record in self.db.get_active_tempbans():
                try:
                    guild = self.bot.get_guild(record["guild_id"])
                    if not guild:
                        self.db.clear_tempban(record["id"])
                        continue

                    if record.get("case_id") and self.db.has_pending_moderation_action(
                        guild.id,
                        record["user_id"],
                        ["TEMPBAN_EXPIRE", "UNBAN"],
                    ):
                        continue

                    start = datetime.fromisoformat(record["ban_start"])
                    expiry = start + timedelta(seconds=record["ban_duration"])

                    if datetime.now(timezone.utc) >= expiry:
                        try:
                            await guild.unban(
                                discord.Object(id=record["user_id"]),
                                reason="Tempban expirado automáticamente",
                            )
                        except discord.NotFound:
                            pass
                        except discord.Forbidden:
                            logger.warning("Sin permisos para desbanear tempban expirado de %s en %s", record["user_id"], guild.name)
                            continue
                        except discord.HTTPException as exc:
                            logger.warning("Error desbaneando tempban expirado %s en %s: %s", record["user_id"], guild.name, exc)
                            continue

                        self.db.clear_tempban(record["id"])
                        if record.get("case_id"):
                            self.db.update_case_by_id(guild.id, int(record["case_id"]), status="expired")
                        self.db.log_action(
                            guild.id, record["user_id"], self.bot.user.id,
                            "TEMPBAN_EXPIRED", "Tempban expirado automáticamente",
                            parent_case_id=record.get("case_id"),
                            status="expired",
                        )

                        log_embed = discord.Embed(
                            title="Tempban expirado",
                            description=f"<@{record['user_id']}> fue desbaneado automáticamente.",
                            color=discord.Color.green(),
                            timestamp=datetime.now(timezone.utc),
                        )
                        log_embed.set_footer(text=f"ID: {record['user_id']}")
                        await self._send_log(guild, log_embed)
                        logger.info("Tempban expirado: %s en %s", record["user_id"], guild.name)

                except Exception as exc:
                    logger.error("Error al expirar tempban individual: %s", exc, exc_info=True)
        except Exception as exc:
            logger.error("Error en _check_tempbans: %s", exc, exc_info=True)

    @_check_tempbans.before_loop
    async def _before_check_tempbans(self):
        await self.bot.wait_until_ready()

    # ── Tarea: scheduler idempotente de moderación ────────────────────────────

    @tasks.loop(minutes=1)
    async def _check_pending_moderation(self):
        """Procesa expiraciones persistentes para tempban/mute/timeout."""
        now = datetime.now(timezone.utc)
        try:
            pending = self.db.get_due_pending_moderation_actions(now.isoformat())
        except Exception as exc:
            logger.error("Error leyendo pending_moderation_actions: %s", exc, exc_info=True)
            return

        for job in pending:
            action_id = job.get("id")
            try:
                guild = self.bot.get_guild(job["guild_id"])
                if not guild:
                    self.db.update_pending_moderation_action(action_id, "failed", "guild_not_found")
                    continue

                action_type = (job.get("action_type") or "").upper()
                user_id = int(job["user_id"])
                case_id = job.get("case_id")
                member = guild.get_member(user_id)

                if action_type in {"TEMPBAN_EXPIRE", "UNBAN"}:
                    try:
                        await guild.unban(discord.Object(id=user_id), reason="Tempban expirado automáticamente")
                    except discord.NotFound:
                        pass
                    except discord.Forbidden:
                        self.db.update_pending_moderation_action(action_id, "failed", "missing_ban_members")
                        continue
                    self.db.clear_tempbans_for_user(user_id, guild.id)
                    if case_id:
                        self.db.update_case_by_id(guild.id, int(case_id), status="expired")
                    self.db.log_action(
                        guild.id, user_id, self.bot.user.id,
                        "TEMPBAN_EXPIRED", "Tempban expirado automáticamente",
                        parent_case_id=int(case_id) if case_id else None,
                        status="expired",
                    )

                elif action_type in {"MUTE_EXPIRE", "HARDMUTE_EXPIRE", "UNMUTE"}:
                    cfg = self.db.get_config(guild.id)
                    mute_role = guild.get_role(cfg.get("mute_role_id") or 0)
                    if member and mute_role and mute_role in member.roles:
                        try:
                            await member.remove_roles(mute_role, reason="Mute expirado automáticamente")
                        except discord.Forbidden:
                            self.db.update_pending_moderation_action(action_id, "failed", "missing_manage_roles")
                            continue
                    self.db.clear_mute(user_id, guild.id)
                    if case_id:
                        self.db.update_case_by_id(guild.id, int(case_id), status="expired")
                    self.db.log_action(
                        guild.id, user_id, self.bot.user.id,
                        "MUTE_EXPIRED", "Mute expirado automáticamente",
                        parent_case_id=int(case_id) if case_id else None,
                        status="expired",
                    )

                elif action_type in {"TIMEOUT_EXPIRE", "UNTIMEOUT"}:
                    if case_id:
                        self.db.update_case_by_id(guild.id, int(case_id), status="expired")
                    self.db.log_action(
                        guild.id, user_id, self.bot.user.id,
                        "TIMEOUT_EXPIRED", "Timeout expirado automáticamente",
                        parent_case_id=int(case_id) if case_id else None,
                        status="expired",
                    )
                else:
                    self.db.update_pending_moderation_action(action_id, "failed", f"unknown_action:{action_type}")
                    continue

                self.db.update_pending_moderation_action(action_id, "done")
            except Exception as exc:
                logger.error("Error procesando pending moderation %s: %s", action_id, exc, exc_info=True)
                try:
                    self.db.update_pending_moderation_action(action_id, "failed", str(exc)[:500])
                except Exception:
                    pass

    @_check_pending_moderation.before_loop
    async def _before_check_pending_moderation(self):
        await self.bot.wait_until_ready()

    # ── Tarea: reintento de logs críticos ─────────────────────────────────────

    @tasks.loop(minutes=1)
    async def _flush_log_outbox(self):
        """Reintenta logs de moderación/automod que no pudieron enviarse."""
        try:
            rows = self.db.get_pending_log_outbox(limit=25)
        except Exception as exc:
            logger.error("Error leyendo log_outbox: %s", exc, exc_info=True)
            return

        for row in rows:
            try:
                log_id = int(row["id"])
                attempts = int(row.get("attempts") or 0)
                guild = self.bot.get_guild(int(row["guild_id"]))
                if guild is None:
                    self._mark_log_outbox_retry(row, "guild_not_found")
                    continue

                channel_id = row.get("channel_id")
                if not channel_id:
                    srv_cfg = self.db.get_server_config(guild.id)
                    channel_id = srv_cfg.get("modlog_channel")
                if not channel_id:
                    self.db.mark_log_outbox(log_id, "failed", "modlog_channel_not_configured")
                    continue

                channel = guild.get_channel(int(channel_id))
                if channel is None:
                    try:
                        channel = await guild.fetch_channel(int(channel_id))
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                        self._mark_log_outbox_retry(row, f"channel_unavailable:{exc}")
                        continue

                if not isinstance(channel, discord.TextChannel):
                    self.db.mark_log_outbox(log_id, "failed", "target_is_not_text_channel")
                    continue

                try:
                    payload = json.loads(row.get("payload") or "{}")
                except json.JSONDecodeError:
                    self.db.mark_log_outbox(log_id, "failed", "invalid_payload_json")
                    continue

                embed_data = payload.get("embed")
                if not isinstance(embed_data, dict):
                    self.db.mark_log_outbox(log_id, "failed", "missing_embed_payload")
                    continue

                await channel.send(embed=discord.Embed.from_dict(embed_data))
                self.db.mark_log_outbox(log_id, "sent")
                logger.info("log_outbox enviado: id=%s attempts=%s", log_id, attempts)
            except (discord.Forbidden, discord.HTTPException) as exc:
                self._mark_log_outbox_retry(row, str(exc))
            except Exception as exc:
                logger.error("Error procesando log_outbox %s: %s", row.get("id"), exc, exc_info=True)
                self._mark_log_outbox_retry(row, str(exc))

    def _mark_log_outbox_retry(self, row: Dict, error: str, max_attempts: int = 5) -> None:
        """Mantiene pendiente un log hasta max_attempts; luego lo marca failed."""
        try:
            attempts = int(row.get("attempts") or 0)
            status = "failed" if attempts + 1 >= max_attempts else "pending"
            self.db.mark_log_outbox(int(row["id"]), status, error[:500])
        except Exception as exc:
            logger.warning("No se pudo actualizar log_outbox %s: %s", row.get("id"), exc)

    @_flush_log_outbox.before_loop
    async def _before_flush_log_outbox(self):
        await self.bot.wait_until_ready()

    # ─────────────────────────────────────────────────────────────────────────
    # /ban
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="ban", description="Banea a un usuario del servidor")
    @app_commands.describe(
        usuario="Usuario a banear",
        razon="Razón del ban",
        duracion="Opcional: si se indica, el ban será temporal. Ej: 30m, 2h, 1d",
        eliminar_mensajes="Días de mensajes a eliminar (0-7, por defecto 0)",
        evidencia_url="URL opcional con evidencia",
    )
    async def ban(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        razon: str = "Sin razón especificada",
        duracion: Optional[str] = None,
        eliminar_mensajes: app_commands.Range[int, 0, 7] = 0,
        evidencia_url: Optional[str] = None,
    ):
        if not self._has_mod_perms(interaction, "ban_members"):
            return await interaction.response.send_message(_voice(self).line("error", "Este comando es solo para mods. El gato te miró feo."), ephemeral=True)

        err = self._can_moderate(interaction.user, usuario)  # type: ignore
        if err:
            return await interaction.response.send_message(_voice(self).line("error", err), ephemeral=True)

        await interaction.response.defer()

        duration_secs: Optional[int] = None
        expiry: Optional[datetime] = None
        if duracion:
            duration_secs = parse_duration(duracion)
            if duration_secs is None:
                return await interaction.followup.send(
                    _voice(self).line("error", "Formato inválido. Ejemplos: `30m` · `2h` · `1d` · `1w`"),
                    ephemeral=True,
                )
            expiry = datetime.now(timezone.utc) + timedelta(seconds=duration_secs)

        action_type = "TEMPBAN" if duration_secs else "BAN"

        # Enviar DM con opción de apelación
        view = AppealUserView(self.bot, interaction.guild_id, action_type, razon)
        await self._dm(
            usuario,
            discord.Embed(
                title="Has sido baneado temporalmente" if duration_secs else "Has sido baneado",
                description=f"Has sido baneado de **{interaction.guild.name}**.",
                color=discord.Color.dark_red(),
            ).add_field(name="Duración", value=fmt_duration(duration_secs))
             .add_field(name="Razón", value=razon)
             .add_field(name="Moderador", value=interaction.user.display_name),
            view=view
        )

        try:
            await usuario.ban(
                reason=f"{action_type} {fmt_duration(duration_secs) if duration_secs else ''} | {razon} | Mod: {interaction.user}",
                delete_message_seconds=eliminar_mensajes * 86400,
            )
        except discord.Forbidden:
            logger.warning("Sin permisos para banear a %s en %s", usuario, interaction.guild)
            return await interaction.followup.send(
                _voice(self).line("error", "Al gato le faltan permisos para banear a este usuario."),
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            logger.warning("Error baneando a %s en %s: %s", usuario, interaction.guild, exc)
            return await interaction.followup.send(
                _voice(self).line("error", "El gato perdió la cuerda. No se pudo completar el ban."),
                ephemeral=True,
            )

        case_id = self.db.log_action(
            interaction.guild_id, usuario.id, interaction.user.id,
            action_type, razon, {"delete_days": eliminar_mensajes, "duration_secs": duration_secs},
            evidence_url=evidencia_url,
            duration_seconds=duration_secs,
            expires_at=expiry.isoformat() if expiry else None,
        )
        if duration_secs and expiry:
            self.db.set_tempban(
                interaction.guild_id, usuario.id, interaction.user.id,
                razon, duration_secs, case_id=case_id,
            )
            self.db.add_pending_moderation_action(
                interaction.guild_id, usuario.id, "TEMPBAN_EXPIRE", expiry.isoformat(), case_id=case_id,
            )

        embed = discord.Embed(
            title="Usuario baneado temporalmente" if duration_secs else "Usuario baneado",
            description=(
                f"**{usuario}** ha sido baneado por **{fmt_duration(duration_secs)}**."
                if duration_secs else f"**{usuario}** ha sido baneado permanentemente."
            ),
            color=discord.Color.dark_red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=usuario.display_avatar.url)
        embed.add_field(name="Usuario", value=f"{usuario.mention}\n`{usuario.id}`", inline=True)
        embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="Msgs eliminados", value=f"{eliminar_mensajes} día(s)", inline=True)
        if duration_secs and expiry:
            embed.add_field(name="⏱️ Duración", value=fmt_duration(duration_secs), inline=True)
            embed.add_field(name="Expira", value=f"<t:{int(expiry.timestamp())}:R>", inline=True)
        if evidencia_url:
            embed.add_field(name="Evidencia", value=evidencia_url, inline=False)
        embed.add_field(name="Razón", value=razon, inline=False)

        await interaction.followup.send(embed=embed)
        await self._send_log(interaction.guild, embed)

    @ban.error
    async def ban_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await self._handle_perm_error(interaction, error)

    @app_commands.command(name="forceban", description="Banea por ID aunque el usuario no esté en el servidor")
    @app_commands.describe(
        user_id="ID del usuario",
        razon="Razón del forceban",
        duracion="Opcional: duración del ban temporal",
        eliminar_mensajes="Días de mensajes a eliminar (0-7)",
    )
    async def forceban(
        self,
        interaction: discord.Interaction,
        user_id: str,
        razon: str = "Sin razón especificada",
        duracion: Optional[str] = None,
        eliminar_mensajes: app_commands.Range[int, 0, 7] = 0,
    ):
        if not self._has_mod_perms(interaction, "ban_members"):
            return await interaction.response.send_message(_voice(self).line("error", "Este comando es solo para mods. El gato te miró feo."), ephemeral=True)
        try:
            uid = int(user_id.strip())
        except ValueError:
            return await interaction.response.send_message("❌ Esa ID no se parece a un ID de Discord.", ephemeral=True)

        secs = parse_duration(duracion) if duracion else None
        if duracion and secs is None:
            return await interaction.response.send_message("❌ Duración inválida. Ejemplos: `30m`, `2h`, `1d`.", ephemeral=True)
        expiry = datetime.now(timezone.utc) + timedelta(seconds=secs) if secs else None

        await interaction.response.defer()
        try:
            await interaction.guild.ban(
                discord.Object(id=uid),
                reason=f"Forceban {fmt_duration(secs) if secs else ''} | {razon} | Mod: {interaction.user}",
                delete_message_seconds=eliminar_mensajes * 86400,
            )
        except discord.Forbidden:
            return await interaction.followup.send("❌ No tengo permisos para banear por ID.", ephemeral=True)
        except discord.HTTPException as exc:
            return await interaction.followup.send(f"❌ No se pudo completar el forceban: {exc}", ephemeral=True)

        action = "TEMPBAN" if secs else "FORCEBAN"
        case_id = self.db.log_action(
            interaction.guild_id, uid, interaction.user.id,
            action, razon, {"forceban": True, "delete_days": eliminar_mensajes, "duration_secs": secs},
            duration_seconds=secs,
            expires_at=expiry.isoformat() if expiry else None,
        )
        if secs and expiry:
            self.db.set_tempban(interaction.guild_id, uid, interaction.user.id, razon, secs, case_id=case_id)
            self.db.add_pending_moderation_action(interaction.guild_id, uid, "TEMPBAN_EXPIRE", expiry.isoformat(), case_id=case_id)

        embed = discord.Embed(
            title="Forceban aplicado",
            description=f"ID `{uid}` fue baneada" + (f" por **{fmt_duration(secs)}**." if secs else "."),
            color=discord.Color.dark_red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="Msgs eliminados", value=f"{eliminar_mensajes} día(s)", inline=True)
        if expiry:
            embed.add_field(name="Expira", value=f"<t:{int(expiry.timestamp())}:R>", inline=True)
        embed.add_field(name="Razón", value=razon, inline=False)
        await interaction.followup.send(embed=embed)
        await self._send_log(interaction.guild, embed)

    @app_commands.command(name="softban", description="Banea y desbanea para limpiar mensajes")
    @app_commands.describe(usuario="Usuario", razon="Razón", eliminar_mensajes="Días de mensajes a eliminar (0-7)")
    async def softban(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        razon: str = "Sin razón especificada",
        eliminar_mensajes: app_commands.Range[int, 0, 7] = 1,
    ):
        if not self._has_mod_perms(interaction, "ban_members"):
            return await interaction.response.send_message(_voice(self).line("error", "Este comando es solo para mods. El gato te miró feo."), ephemeral=True)
        err = self._can_moderate(interaction.user, usuario)  # type: ignore
        if err:
            return await interaction.response.send_message(_voice(self).line("error", err), ephemeral=True)
        await interaction.response.defer()
        try:
            await usuario.ban(reason=f"Softban: {razon} | Mod: {interaction.user}", delete_message_seconds=eliminar_mensajes * 86400)
            await interaction.guild.unban(usuario, reason=f"Softban cleanup completado | Mod: {interaction.user}")
        except discord.Forbidden:
            return await interaction.followup.send("❌ No tengo permisos/jerarquía para softban.", ephemeral=True)
        except discord.HTTPException as exc:
            return await interaction.followup.send(f"❌ No se pudo completar el softban: {exc}", ephemeral=True)
        self.db.log_action(
            interaction.guild_id, usuario.id, interaction.user.id,
            "SOFTBAN", razon, {"delete_days": eliminar_mensajes},
            status="expired",
        )
        embed = discord.Embed(
            title="Softban aplicado",
            description=f"{usuario.mention} fue baneado y desbaneado para limpiar mensajes.",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Mensajes eliminados", value=f"{eliminar_mensajes} día(s)", inline=True)
        embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="Razón", value=razon, inline=False)
        await interaction.followup.send(embed=embed)
        await self._send_log(interaction.guild, embed)

    # ─────────────────────────────────────────────────────────────────────────
    # /tempban
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="tempban", description="Banea temporalmente a un usuario del servidor")
    @app_commands.describe(
        usuario="Usuario a banear temporalmente",
        duracion="Duración: 30m, 2h, 1d, 1w",
        razon="Razón del tempban",
        eliminar_mensajes="Días de mensajes a eliminar (0-7, por defecto 0)",
    )
    async def tempban(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        duracion: str,
        razon: str = "Sin razón especificada",
        eliminar_mensajes: app_commands.Range[int, 0, 7] = 0,
    ):
        if not self._has_mod_perms(interaction, "ban_members"):
            return await interaction.response.send_message(_voice(self).line("error", "Este comando es solo para mods. El gato te miró feo."), ephemeral=True)

        err = self._can_moderate(interaction.user, usuario)
        if err:
            return await interaction.response.send_message(_voice(self).line("error", err), ephemeral=True)

        secs = parse_duration(duracion)
        if secs is None:
            return await interaction.response.send_message(
                _voice(self).line("error", "Formato inválido. Ejemplos: `30m` · `2h` · `1d` · `1w`"),
                ephemeral=True,
            )

        await interaction.response.defer()

        view = AppealUserView(self.bot, interaction.guild_id, "TEMPBAN", razon)
        await self._dm(
            usuario,
            discord.Embed(
                title="Has sido baneado temporalmente",
                description=f"Has sido baneado temporalmente de **{interaction.guild.name}**.",
                color=discord.Color.dark_red(),
            ).add_field(name="Duración", value=fmt_duration(secs))
             .add_field(name="Razón", value=razon)
             .add_field(name="Moderador", value=interaction.user.display_name),
            view=view
        )

        try:
            await usuario.ban(
                reason=f"Tempban {fmt_duration(secs)} | {razon} | Mod: {interaction.user}",
                delete_message_seconds=eliminar_mensajes * 86400,
            )
        except discord.Forbidden:
            logger.warning("Sin permisos para tempbanear a %s en %s", usuario, interaction.guild)
            return await interaction.followup.send(
                _voice(self).line("error", "Al gato le faltan permisos para banear a este usuario."),
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            logger.warning("Error tempbaneando a %s en %s: %s", usuario, interaction.guild, exc)
            return await interaction.followup.send(
                _voice(self).line("error", "El gato perdió la cuerda. No se pudo completar el tempban."),
                ephemeral=True,
            )

        expiry = datetime.now(timezone.utc) + timedelta(seconds=secs)
        case_id = self.db.log_action(
            interaction.guild_id, usuario.id, interaction.user.id,
            "TEMPBAN", razon, {"duration_secs": secs, "delete_days": eliminar_mensajes},
            duration_seconds=secs,
            expires_at=expiry.isoformat(),
        )
        self.db.set_tempban(
            interaction.guild_id, usuario.id, interaction.user.id,
            razon, secs, case_id=case_id,
        )
        self.db.add_pending_moderation_action(
            interaction.guild_id, usuario.id, "TEMPBAN_EXPIRE", expiry.isoformat(), case_id=case_id,
        )

        embed = discord.Embed(
            title="Usuario baneado temporalmente",
            description=f"**{usuario}** ha sido baneado por **{fmt_duration(secs)}**.",
            color=discord.Color.dark_red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=usuario.display_avatar.url)
        embed.add_field(name="Usuario", value=f"{usuario.mention}\n`{usuario.id}`", inline=True)
        embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="⏱️ Duración", value=fmt_duration(secs), inline=True)
        embed.add_field(name="Msgs eliminados", value=f"{eliminar_mensajes} día(s)", inline=True)
        embed.add_field(name="Razón", value=razon, inline=False)
        embed.add_field(name="Expira", value=f"<t:{int(expiry.timestamp())}:R>", inline=False)

        await interaction.followup.send(embed=embed)
        await self._send_log(interaction.guild, embed)

    @tempban.error
    async def tempban_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await self._handle_perm_error(interaction, error)

    # ─────────────────────────────────────────────────────────────────────────
    # /unban
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="unban", description="Desbanea un usuario usando su ID")
    @app_commands.describe(
        user_id="ID numérica del usuario a desbanear",
        razon="Razón del desbaneo",
    )
    async def unban(
        self,
        interaction: discord.Interaction,
        user_id: str,
        razon: str = "Sin razón especificada",
    ):
        if not self._has_mod_perms(interaction, "ban_members"):
            return await interaction.response.send_message(_voice(self).line("error", "Este comando es solo para mods. El gato te miró feo."), ephemeral=True)

        await interaction.response.defer()

        try:
            uid = int(user_id.strip())
        except ValueError:
            return await interaction.followup.send(_voice(self).line("error", "Esa ID no se parece a un ID de Discord."), ephemeral=True)

        try:
            entry = await interaction.guild.fetch_ban(discord.Object(id=uid))
        except discord.NotFound:
            return await interaction.followup.send(
                f"❌ No existe un ban activo con ID `{uid}`.", ephemeral=True
            )

        try:
            await interaction.guild.unban(entry.user, reason=f"{razon} | Mod: {interaction.user}")
        except discord.Forbidden:
            logger.warning("Sin permisos para desbanear a %s en %s", entry.user, interaction.guild)
            return await interaction.followup.send(
                "❌ No tengo permisos suficientes para desbanear a ese usuario.",
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            logger.warning("Error desbaneando a %s en %s: %s", entry.user, interaction.guild, exc)
            return await interaction.followup.send(
                "❌ No se pudo completar el desbaneo. Inténtalo de nuevo.",
                ephemeral=True,
            )

        self.db.clear_tempbans_for_user(uid, interaction.guild_id)
        self.db.cancel_pending_moderation_actions(
            interaction.guild_id,
            uid,
            ["TEMPBAN_EXPIRE", "UNBAN"],
            reason="manual_unban",
        )
        self.db.revoke_active_moderations(
            interaction.guild_id,
            uid,
            ["BAN", "TEMPBAN", "FORCEBAN"],
        )
        self.db.log_action(interaction.guild_id, uid, interaction.user.id, "UNBAN", razon)

        embed = discord.Embed(
            title="✅ Usuario desbaneado",
            description=f"**{entry.user}** fue desbaneado.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=entry.user.display_avatar.url)
        embed.add_field(name="Usuario", value=f"{entry.user.mention}\n`{uid}`", inline=True)
        embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="Razón", value=razon, inline=False)

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
    async def kick(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        razon: str = "Sin razón especificada",
    ):
        if not self._has_mod_perms(interaction, "kick_members"):
            return await interaction.response.send_message(_voice(self).line("error", "Este comando es solo para mods. El gato te miró feo."), ephemeral=True)

        err = self._can_moderate(interaction.user, usuario)  # type: ignore
        if err:
            return await interaction.response.send_message(_voice(self).line("error", err), ephemeral=True)

        await interaction.response.defer()

        view = AppealUserView(self.bot, interaction.guild_id, "KICK", razon)
        await self._dm(
            usuario,
            discord.Embed(
                title="Has sido expulsado",
                description=f"Has sido expulsado de **{interaction.guild.name}**.",
                color=discord.Color.orange(),
            ).add_field(name="Razón", value=razon)
             .add_field(name="Moderador", value=interaction.user.display_name),
            view=view
        )

        try:
            await usuario.kick(reason=f"{razon} | Mod: {interaction.user}")
        except discord.Forbidden:
            logger.warning("Sin permisos para expulsar a %s en %s", usuario, interaction.guild)
            return await interaction.followup.send(
                "❌ No tengo permisos suficientes para expulsar a ese usuario.",
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            logger.warning("Error expulsando a %s en %s: %s", usuario, interaction.guild, exc)
            return await interaction.followup.send(
                "❌ No se pudo completar la expulsión. Inténtalo de nuevo.",
                ephemeral=True,
            )

        self.db.log_action(interaction.guild_id, usuario.id, interaction.user.id, "KICK", razon)

        embed = discord.Embed(
            title="Usuario expulsado",
            description=f"**{usuario}** fue expulsado del servidor.",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=usuario.display_avatar.url)
        embed.add_field(name="Usuario", value=f"{usuario.mention}\n`{usuario.id}`", inline=True)
        embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="Razón", value=razon, inline=False)

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
    async def mute(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        duracion: Optional[str] = None,
        razon: str = "Sin razón especificada",
    ):
        await interaction.response.defer(thinking=True)
        guild = interaction.guild
        if guild is None:
            return await interaction.followup.send("❌ Este comando solo funciona en servidores.", ephemeral=True)

        if not self._has_mod_perms(interaction, "manage_roles"):
            return await interaction.followup.send(_voice(self).line("error", "Este comando es solo para mods. El gato te miró feo."), ephemeral=True)
        mute_role = await self._ensure_mute_role(guild)

        if not mute_role:
            return await interaction.followup.send(
                "❌ No hay rol de mute configurado.\n"
                "Usa `/modconfig mute_role` o el dashboard para asignarlo.",
                ephemeral=True,
            )

        err = self._can_moderate(interaction.user, usuario)  # type: ignore
        if err:
            return await interaction.followup.send(_voice(self).line("error", err), ephemeral=True)

        if mute_role in usuario.roles:
            return await interaction.followup.send(
                f"⚠️ {usuario.mention} ya está silenciado.", ephemeral=True
            )

        secs: Optional[int] = None
        if duracion:
            secs = parse_duration(duracion)
            if secs is None:
                return await interaction.followup.send(
                    _voice(self).line("error", "Formato inválido. Ejemplos: `30m` · `2h` · `1d` · `1w`"),
                    ephemeral=True,
                )

        try:
            await usuario.add_roles(mute_role, reason=f"Mute: {razon} | Mod: {interaction.user}")
        except discord.Forbidden:
            logger.warning("Sin permisos para mutear a %s en %s", usuario, interaction.guild)
            return await interaction.followup.send(
                "❌ No tengo permisos suficientes para aplicar el rol de mute.",
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            logger.warning("Error muteando a %s en %s: %s", usuario, interaction.guild, exc)
            return await interaction.followup.send(
                "❌ No se pudo completar el mute. Inténtalo de nuevo.",
                ephemeral=True,
            )

        self.db.set_mute(usuario.id, interaction.guild_id, secs)
        expiry = datetime.now(timezone.utc) + timedelta(seconds=secs) if secs else None
        case_id = self.db.log_action(
            interaction.guild_id, usuario.id, interaction.user.id,
            "MUTE", razon, {"duration_secs": secs},
            duration_seconds=secs,
            expires_at=expiry.isoformat() if expiry else None,
        )
        if expiry:
            self.db.add_pending_moderation_action(
                interaction.guild_id, usuario.id, "MUTE_EXPIRE", expiry.isoformat(), case_id=case_id,
            )

        embed = discord.Embed(
            title="Usuario silenciado",
            description=f"{usuario.mention} ha sido silenciado.",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Usuario", value=f"{usuario.mention}\n`{usuario.id}`", inline=True)
        embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="⏱️ Duración", value=fmt_duration(secs), inline=True)
        if expiry:
            embed.add_field(name="Expira", value=f"<t:{int(expiry.timestamp())}:R>", inline=True)
        embed.add_field(name="Razón", value=razon, inline=False)

        await interaction.followup.send(embed=embed)
        await self._send_log(interaction.guild, embed)

        view = AppealUserView(self.bot, interaction.guild_id, "MUTE", razon)
        await self._dm(
            usuario,
            discord.Embed(
                title="Has sido silenciado",
                description=f"Has sido silenciado en **{interaction.guild.name}**.",
                color=discord.Color.red(),
            ).add_field(name="Duración", value=fmt_duration(secs))
             .add_field(name="Razón", value=razon),
            view=view
        )

    @mute.error
    async def mute_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await self._handle_perm_error(interaction, error)

    @app_commands.command(name="hardmute", description="Silencia creando/adoptando rol Muted y persistiendo expiración")
    @app_commands.describe(
        usuario="Usuario a silenciar",
        duracion="Duración: 30m, 2h, 1d, 1w — omitir para permanente",
        razon="Razón del hardmute",
    )
    async def hardmute(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        duracion: Optional[str] = None,
        razon: str = "Sin razón especificada",
    ):
        if not self._has_mod_perms(interaction, "manage_roles"):
            return await interaction.response.send_message(_voice(self).line("error", "Este comando es solo para mods. El gato te miró feo."), ephemeral=True)
        err = self._can_moderate(interaction.user, usuario)  # type: ignore
        if err:
            return await interaction.response.send_message(_voice(self).line("error", err), ephemeral=True)
        secs = parse_duration(duracion) if duracion else None
        if duracion and secs is None:
            return await interaction.response.send_message("❌ Duración inválida. Ejemplos: `30m`, `2h`, `1d`.", ephemeral=True)

        await interaction.response.defer()
        mute_role = await self._ensure_mute_role(interaction.guild)
        if not mute_role:
            return await interaction.followup.send("❌ No pude crear/adoptar el rol Muted. Revisa `Gestionar roles` y jerarquía.", ephemeral=True)
        if mute_role in usuario.roles:
            return await interaction.followup.send(f"⚠️ {usuario.mention} ya está silenciado.", ephemeral=True)
        try:
            await usuario.add_roles(mute_role, reason=f"Hardmute: {razon} | Mod: {interaction.user}")
        except discord.Forbidden:
            return await interaction.followup.send("❌ No tengo permisos/jerarquía para aplicar hardmute.", ephemeral=True)
        except discord.HTTPException as exc:
            return await interaction.followup.send(f"❌ No se pudo aplicar hardmute: {exc}", ephemeral=True)

        expiry = datetime.now(timezone.utc) + timedelta(seconds=secs) if secs else None
        self.db.set_mute(usuario.id, interaction.guild_id, secs)
        case_id = self.db.log_action(
            interaction.guild_id, usuario.id, interaction.user.id,
            "HARDMUTE", razon, {"duration_secs": secs},
            duration_seconds=secs,
            expires_at=expiry.isoformat() if expiry else None,
        )
        if expiry:
            self.db.add_pending_moderation_action(
                interaction.guild_id, usuario.id, "HARDMUTE_EXPIRE", expiry.isoformat(), case_id=case_id,
            )

        embed = discord.Embed(
            title="Hardmute aplicado",
            description=f"{usuario.mention} fue silenciado con rol Muted.",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Usuario", value=f"{usuario.mention}\n`{usuario.id}`", inline=True)
        embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="Duración", value=fmt_duration(secs), inline=True)
        if expiry:
            embed.add_field(name="Expira", value=f"<t:{int(expiry.timestamp())}:R>", inline=True)
        embed.add_field(name="Razón", value=razon, inline=False)
        await interaction.followup.send(embed=embed)
        await self._send_log(interaction.guild, embed)

    # ─────────────────────────────────────────────────────────────────────────
    # /unmute
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="unmute", description="Quita el silencio a un usuario")
    @app_commands.describe(usuario="Usuario a desilenciar", razon="Razón del unmute")
    async def unmute(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        razon: str = "Sin razón especificada",
    ):
        if not self._has_mod_perms(interaction, "manage_roles"):
            return await interaction.response.send_message(_voice(self).line("error", "Este comando es solo para mods. El gato te miró feo."), ephemeral=True)
        mute_role = await self._ensure_mute_role(interaction.guild)

        if not mute_role:
            return await interaction.response.send_message(
                "❌ No hay rol de mute configurado. Usa `/modconfig mute_role` o el dashboard.", ephemeral=True
            )

        if mute_role not in usuario.roles:
            return await interaction.response.send_message(
                f"⚠️ {usuario.mention} no está silenciado.", ephemeral=True
            )

        try:
            await usuario.remove_roles(mute_role, reason=f"Unmute: {razon} | Mod: {interaction.user}")
        except discord.Forbidden:
            logger.warning("Sin permisos para quitar mute a %s en %s", usuario, interaction.guild)
            return await interaction.response.send_message(
                "❌ No tengo permisos suficientes para quitar el rol de mute.",
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            logger.warning("Error quitando mute a %s en %s: %s", usuario, interaction.guild, exc)
            return await interaction.response.send_message(
                "❌ No se pudo completar el unmute. Inténtalo de nuevo.",
                ephemeral=True,
            )

        self.db.clear_mute(usuario.id, interaction.guild_id)
        self.db.cancel_pending_moderation_actions(
            interaction.guild_id,
            usuario.id,
            ["MUTE_EXPIRE", "HARDMUTE_EXPIRE", "UNMUTE"],
            reason="manual_unmute",
        )
        self.db.revoke_active_moderations(
            interaction.guild_id,
            usuario.id,
            ["MUTE", "AUTO_MUTE", "HARDMUTE"],
        )
        self.db.log_action(interaction.guild_id, usuario.id, interaction.user.id, "UNMUTE", razon, status="revoked")

        embed = discord.Embed(
            title="Usuario desilenciado",
            description=f"{usuario.mention} fue desilenciado.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Usuario", value=usuario.mention, inline=True)
        embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="Razón", value=razon, inline=False)

        await interaction.response.send_message(embed=embed)
        await self._send_log(interaction.guild, embed)

        await self._dm(
            usuario,
            discord.Embed(
                title="Fuiste desilenciado",
                description=f"Tu silencio en **{interaction.guild.name}** fue levantado.",
                color=discord.Color.green(),
            ).add_field(name="Razón", value=razon),
        )

    @unmute.error
    async def unmute_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await self._handle_perm_error(interaction, error)

    # ─────────────────────────────────────────────────────────────────────────
    # /timeout y /untimeout
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="timeout", description="Aplica timeout nativo de Discord a un usuario")
    @app_commands.describe(
        usuario="Usuario a timeoutear",
        duracion="Duración: 30m, 2h, 1d. Máximo 28d por Discord",
        razon="Razón del timeout",
        evidencia_url="URL opcional con evidencia",
    )
    async def timeout_cmd(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        duracion: str,
        razon: str = "Sin razón especificada",
        evidencia_url: Optional[str] = None,
    ):
        if not await self._safe_defer(interaction):
            return
        if not self._has_mod_perms(interaction, "moderate_members"):
            await self._safe_interaction_send(
                interaction,
                _voice(self).line("error", "Este comando es solo para mods. El gato te miró feo."),
                ephemeral=True,
            )
            return
        err = self._can_moderate(interaction.user, usuario)  # type: ignore
        if err:
            await self._safe_interaction_send(interaction, _voice(self).line("error", err), ephemeral=True)
            return

        secs = parse_duration(duracion)
        max_timeout = 28 * 86400
        if secs is None or secs <= 0:
            await self._safe_interaction_send(interaction, "❌ Duración inválida. Ejemplos: `30m`, `2h`, `1d`.", ephemeral=True)
            return
        if secs > max_timeout:
            await self._safe_interaction_send(
                interaction,
                "❌ Discord solo permite timeouts de hasta 28 días. Usa `/mute` para duraciones más largas.",
                ephemeral=True,
            )
            return

        until = datetime.now(timezone.utc) + timedelta(seconds=secs)
        try:
            await usuario.timeout(until, reason=f"Timeout: {razon} | Mod: {interaction.user}")
        except discord.Forbidden:
            await self._safe_interaction_send(interaction, "❌ No tengo permisos/jerarquía para aplicar timeout.", ephemeral=True)
            return
        except discord.HTTPException as exc:
            await self._safe_interaction_send(interaction, f"❌ No se pudo aplicar timeout: {exc}", ephemeral=True)
            return

        case_id = self.db.log_action(
            interaction.guild_id, usuario.id, interaction.user.id,
            "TIMEOUT", razon, {"duration_secs": secs},
            evidence_url=evidencia_url,
            duration_seconds=secs,
            expires_at=until.isoformat(),
        )
        self.db.add_pending_moderation_action(
            interaction.guild_id, usuario.id, "TIMEOUT_EXPIRE", until.isoformat(), case_id=case_id,
        )

        embed = discord.Embed(
            title="Timeout aplicado",
            description=f"{usuario.mention} fue aislado por **{fmt_duration(secs)}**.",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Usuario", value=f"{usuario.mention}\n`{usuario.id}`", inline=True)
        embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="Expira", value=f"<t:{int(until.timestamp())}:R>", inline=True)
        if evidencia_url:
            embed.add_field(name="Evidencia", value=evidencia_url, inline=False)
        embed.add_field(name="Razón", value=razon, inline=False)
        await self._safe_interaction_send(interaction, embed=embed)
        await self._send_log(interaction.guild, embed)

    @timeout_cmd.error
    async def timeout_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await self._handle_perm_error(interaction, error)

    @app_commands.command(name="untimeout", description="Quita el timeout nativo de Discord a un usuario")
    @app_commands.describe(usuario="Usuario", razon="Razón")
    async def untimeout(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        razon: str = "Sin razón especificada",
    ):
        if not await self._safe_defer(interaction):
            return
        if not self._has_mod_perms(interaction, "moderate_members"):
            await self._safe_interaction_send(
                interaction,
                _voice(self).line("error", "Este comando es solo para mods. El gato te miró feo."),
                ephemeral=True,
            )
            return
        err = self._can_moderate(interaction.user, usuario)  # type: ignore
        if err:
            await self._safe_interaction_send(interaction, _voice(self).line("error", err), ephemeral=True)
            return
        try:
            await usuario.timeout(None, reason=f"Untimeout: {razon} | Mod: {interaction.user}")
        except discord.Forbidden:
            await self._safe_interaction_send(interaction, "❌ No tengo permisos/jerarquía para quitar timeout.", ephemeral=True)
            return
        except discord.HTTPException as exc:
            await self._safe_interaction_send(interaction, f"❌ No se pudo quitar timeout: {exc}", ephemeral=True)
            return

        self.db.cancel_pending_moderation_actions(
            interaction.guild_id,
            usuario.id,
            ["TIMEOUT_EXPIRE", "UNTIMEOUT"],
            reason="manual_untimeout",
        )
        self.db.revoke_active_moderations(
            interaction.guild_id,
            usuario.id,
            ["TIMEOUT"],
        )
        self.db.log_action(interaction.guild_id, usuario.id, interaction.user.id, "UNTIMEOUT", razon, status="revoked")
        embed = discord.Embed(
            title="Timeout retirado",
            description=f"{usuario.mention} ya no está en timeout.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Usuario", value=usuario.mention, inline=True)
        embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="Razón", value=razon, inline=False)
        await self._safe_interaction_send(interaction, embed=embed)
        await self._send_log(interaction.guild, embed)

    @untimeout.error
    async def untimeout_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await self._handle_perm_error(interaction, error)

    # ─────────────────────────────────────────────────────────────────────────
    # /warn
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="warn", description="Advierte a un usuario (con consecuencias configurables)")
    @app_commands.describe(
        usuario="Usuario a advertir",
        razon="Razón de la advertencia",
        evidencia_url="URL opcional con evidencia",
    )
    async def warn(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        razon: str = "Sin razón especificada",
        evidencia_url: Optional[str] = None,
    ):
        if not self._has_mod_perms(interaction, "moderate_members"):
            return await interaction.response.send_message(_voice(self).line("error", "Este comando es solo para mods. El gato te miró feo."), ephemeral=True)
        err = self._can_moderate(interaction.user, usuario)  # type: ignore
        if err:
            return await interaction.response.send_message(_voice(self).line("error", err), ephemeral=True)

        await interaction.response.defer()

        cfg = self.db.get_config(interaction.guild_id)
        warns = self.db.add_warn(usuario.id, interaction.guild_id)
        warn_case_id = self.db.log_action(
            interaction.guild_id, usuario.id, interaction.user.id, "WARN", razon,
            evidence_url=evidencia_url,
        )

        # Embed de warn (configurable)
        warn_embed = build_warn_embed(cfg, usuario, interaction.user, razon, warns)  # type: ignore
        if evidencia_url:
            warn_embed.add_field(name="Evidencia", value=evidencia_url, inline=False)
        await interaction.followup.send(embed=warn_embed)
        log_ok = await self._send_log(interaction.guild, warn_embed)
        if not log_ok:
            await self._notify_modlog_issue(interaction)

        view = AppealUserView(self.bot, interaction.guild_id, "WARN", razon)
        await self._dm(usuario, warn_embed, view=view)

        # ── Consecuencias automáticas (de mayor a menor severidad) ────────────

        ban_thr   = cfg.get("warn_ban_threshold", 7)
        kick_thr  = cfg.get("warn_kick_threshold", 5)
        mute_thr  = cfg.get("warn_mute_threshold", 3)
        ban_on    = bool(cfg.get("warn_ban_enabled", 0))
        kick_on   = bool(cfg.get("warn_kick_enabled", 0))
        mute_on   = bool(cfg.get("warn_mute_enabled", 1))

        consequence_embed: Optional[discord.Embed] = None
        consequence_warning: Optional[str] = None

        # Ban automático
        if ban_on and warns >= ban_thr:
            try:
                await usuario.ban(reason=f"Auto-ban: alcanzó {warns} warns")
                self.db.log_action(
                    interaction.guild_id, usuario.id, self.bot.user.id,
                    "AUTO_BAN", f"Alcanzó {warns} warns",
                    parent_case_id=warn_case_id,
                )
                consequence_embed = discord.Embed(
                    title="Ban automático",
                    description=f"{usuario.mention} fue baneado por alcanzar **{warns} warns**.",
                    color=discord.Color.dark_red(),
                    timestamp=datetime.now(timezone.utc),
                )
            except discord.Forbidden:
                logger.warning("Sin permisos para auto-ban de %s", usuario)
                consequence_warning = (
                    "⚠️ Auto-ban falló: el bot no tiene permiso `Banear miembros`"
                    " o su rol está debajo del usuario."
                )

        # Kick automático (solo si no se baneó)
        elif kick_on and warns >= kick_thr:
            try:
                await usuario.kick(reason=f"Auto-kick: alcanzó {warns} warns")
                self.db.log_action(
                    interaction.guild_id, usuario.id, self.bot.user.id,
                    "AUTO_KICK", f"Alcanzó {warns} warns",
                    parent_case_id=warn_case_id,
                )
                consequence_embed = discord.Embed(
                    title="Kick automático",
                    description=f"{usuario.mention} fue expulsado por alcanzar **{warns} warns**.",
                    color=discord.Color.dark_orange(),
                    timestamp=datetime.now(timezone.utc),
                )
            except discord.Forbidden:
                logger.warning("Sin permisos para auto-kick de %s", usuario)
                consequence_warning = (
                    "⚠️ Auto-kick falló: el bot no tiene permiso `Expulsar miembros`"
                    " o su rol está debajo del usuario."
                )

        # Mute automático (solo si no se baneó ni kickeó)
        elif mute_on and warns >= mute_thr:
            mute_role = await self._ensure_mute_role(interaction.guild)
            if not mute_role:
                consequence_warning = (
                    f"⚠️ Auto-mute en {warns} warns no se aplicó: no hay rol de mute"
                    " y el bot no puede crearlo (falta permiso `Gestionar roles`)."
                    " Configúralo manualmente en el dashboard → Moderación."
                )
            elif mute_role in usuario.roles:
                consequence_warning = (
                    f"ℹ️ {usuario.mention} ya tenía el rol de mute; no se re-aplicó."
                )
            else:
                dur = int(cfg.get("warn_mute_duration", 3600) or 3600)
                try:
                    await usuario.add_roles(
                        mute_role, reason=f"Auto-mute: alcanzó {warns} warns"
                    )
                    self.db.set_mute(usuario.id, interaction.guild_id, dur)
                    expires_at = datetime.now(timezone.utc) + timedelta(seconds=dur)
                    auto_mute_case_id = self.db.log_action(
                        interaction.guild_id, usuario.id, self.bot.user.id,
                        "AUTO_MUTE", f"Alcanzó {warns} warns", {"duration_secs": dur},
                        duration_seconds=dur,
                        expires_at=expires_at.isoformat(),
                        parent_case_id=warn_case_id,
                    )
                    self.db.add_pending_moderation_action(
                        interaction.guild_id,
                        usuario.id,
                        "MUTE_EXPIRE",
                        expires_at.isoformat(),
                        case_id=auto_mute_case_id,
                    )
                    consequence_embed = discord.Embed(
                        title="Mute automático",
                        description=(
                            f"{usuario.mention} fue silenciado por alcanzar **{warns} warns**.\n"
                            f"Duración: **{fmt_duration(dur)}**"
                        ),
                        color=discord.Color.red(),
                        timestamp=datetime.now(timezone.utc),
                    )
                except discord.Forbidden:
                    logger.warning("Sin permisos para auto-mute de %s", usuario)
                    consequence_warning = (
                        "⚠️ Auto-mute falló: el rol del bot está debajo del rol de mute"
                        " o falta permiso `Gestionar roles`."
                    )

        if consequence_embed:
            await interaction.followup.send(embed=consequence_embed)
            log_ok = await self._send_log(interaction.guild, consequence_embed)
            if not log_ok:
                await self._notify_modlog_issue(interaction)

        if consequence_warning:
            try:
                await interaction.followup.send(consequence_warning, ephemeral=True)
            except discord.HTTPException:
                pass

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
            title=f"Warns de {target.display_name}",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="⚠️ Warns actuales", value=f"**{w}**", inline=True)

        next_enabled_thresholds = [t for t, en in thresholds.values() if en and t > w]
        next_threshold = min(next_enabled_thresholds) if next_enabled_thresholds else None

        lines = []
        for name, (thr, enabled) in thresholds.items():
            icon = "✅" if enabled else "❌"
            marker = " ← próximo" if enabled and next_threshold is not None and thr == next_threshold else ""
            lines.append(f"{icon} **{name}** a los {thr} warns{marker}")

        embed.add_field(name="Consecuencias configuradas", value="\n".join(lines), inline=False)
        embed.set_footer(text=f"ID: {target.id}")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /unwarn
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="unwarn", description="Revoca un warn por número de caso")
    @app_commands.describe(case_id="Número de caso o ID interno", razon="Razón de la revocación")
    async def unwarn(
        self,
        interaction: discord.Interaction,
        case_id: int,
        razon: str = "Sin razón especificada",
    ):
        if not self._has_mod_perms(interaction, "moderate_members"):
            return await interaction.response.send_message(_voice(self).line("error", "Este comando es solo para mods. El gato te miró feo."), ephemeral=True)
        case = self.db.get_case(interaction.guild_id, case_id)
        if not case:
            return await interaction.response.send_message(f"❌ No existe el caso `{case_id}`.", ephemeral=True)
        if (case.get("action_type") or "").upper() not in {"WARN", "AUTO_WARN"}:
            return await interaction.response.send_message("❌ Ese caso no es un warn.", ephemeral=True)
        if (case.get("status") or "active") == "revoked":
            return await interaction.response.send_message("ℹ️ Ese warn ya estaba revocado.", ephemeral=True)

        new_total = self.db.decrement_warn(case["target_id"], interaction.guild_id)
        updated = self.db.update_case_by_id(
            interaction.guild_id,
            int(case["id"]),
            status="revoked",
            extra_data=json.dumps({"revoked_by": interaction.user.id, "revoked_reason": razon}, ensure_ascii=False),
        )
        self.db.log_action(
            interaction.guild_id, case["target_id"], interaction.user.id,
            "UNWARN", razon, {"case_id": case.get("id"), "case_number": case.get("case_number")},
            parent_case_id=int(case["id"]),
            status="revoked",
        )

        embed = discord.Embed(
            title="Warn revocado",
            description=f"Caso **#{updated.get('case_number', case_id)}** revocado. Warns activos restantes: **{new_total}**.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Usuario", value=f"<@{case['target_id']}>\n`{case['target_id']}`", inline=True)
        embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="Razón", value=razon, inline=False)
        await interaction.response.send_message(embed=embed)
        await self._send_log(interaction.guild, embed)

    @unwarn.error
    async def unwarn_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await self._handle_perm_error(interaction, error)

    # ─────────────────────────────────────────────────────────────────────────
    # /cases y /moderations
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="cases", description="Muestra historial de casos de moderación")
    @app_commands.describe(usuario="Filtrar por usuario", case_id="Mostrar un caso específico")
    async def cases_cmd(
        self,
        interaction: discord.Interaction,
        usuario: Optional[discord.Member] = None,
        case_id: Optional[int] = None,
    ):
        if not self._has_mod_perms(interaction, "moderate_members"):
            return await interaction.response.send_message(_voice(self).line("error", "Este comando es solo para mods. El gato te miró feo."), ephemeral=True)

        if case_id is not None:
            case = self.db.get_case(interaction.guild_id, case_id)
            if not case:
                return await interaction.response.send_message(f"❌ No existe el caso `{case_id}`.", ephemeral=True)
            embed = self._case_embed(case, title="Detalle de caso")
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        rows = self.db.list_cases(interaction.guild_id, usuario.id if usuario else None, limit=10)
        if not rows:
            return await interaction.response.send_message("No hay casos para mostrar.", ephemeral=True)

        embed = discord.Embed(
            title="Casos de moderación" if not usuario else f"Casos de {usuario.display_name}",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        for row in rows[:10]:
            embed.add_field(
                name=f"#{row.get('case_number') or row.get('id')} · {row.get('action_type')} · {row.get('status') or 'active'}",
                value=f"Usuario: <@{row.get('target_id')}> · Mod: <@{row.get('moderator_id')}>\nRazón: {row.get('reason') or '—'}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @cases_cmd.error
    async def cases_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await self._handle_perm_error(interaction, error)

    @app_commands.command(name="moderations", description="Lista sanciones activas con expiración")
    async def moderations_cmd(self, interaction: discord.Interaction):
        if not self._has_mod_perms(interaction, "moderate_members"):
            return await interaction.response.send_message(_voice(self).line("error", "Este comando es solo para mods. El gato te miró feo."), ephemeral=True)
        rows = self.db.get_active_moderations(interaction.guild_id, limit=15)
        pending = self.db.get_pending_moderation_actions(interaction.guild_id, limit=15)
        if not rows and not pending:
            return await interaction.response.send_message("No hay moderaciones activas registradas.", ephemeral=True)
        embed = discord.Embed(title="Moderaciones activas", color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))
        seen = set()
        for row in rows:
            seen.add(row.get("id"))
            exp = row.get("expires_at")
            exp_txt = "permanente" if not exp else f"<t:{int(datetime.fromisoformat(exp).timestamp())}:R>"
            embed.add_field(
                name=f"#{row.get('case_number') or row.get('id')} · {row.get('action_type')}",
                value=f"Usuario: <@{row.get('target_id')}> · Expira: {exp_txt}\nRazón: {row.get('reason') or '—'}",
                inline=False,
            )
        for job in pending:
            if job.get("case_id") in seen:
                continue
            exp_txt = f"<t:{int(datetime.fromisoformat(job['execute_at']).timestamp())}:R>"
            embed.add_field(
                name=f"Job #{job.get('id')} · {job.get('action_type')}",
                value=f"Usuario: <@{job.get('user_id')}> · Ejecuta: {exp_txt}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    def _case_embed(self, case: Dict, title: str = "Caso") -> discord.Embed:
        embed = discord.Embed(
            title=f"{title} #{case.get('case_number') or case.get('id')}",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Acción", value=str(case.get("action_type") or "—"), inline=True)
        embed.add_field(name="Estado", value=str(case.get("status") or "active"), inline=True)
        embed.add_field(name="Usuario", value=f"<@{case.get('target_id')}>\n`{case.get('target_id')}`", inline=True)
        embed.add_field(name="Moderador", value=f"<@{case.get('moderator_id')}>\n`{case.get('moderator_id')}`", inline=True)
        if case.get("duration_seconds"):
            embed.add_field(name="Duración", value=fmt_duration(int(case["duration_seconds"])), inline=True)
        if case.get("expires_at"):
            try:
                embed.add_field(name="Expira", value=f"<t:{int(datetime.fromisoformat(case['expires_at']).timestamp())}:R>", inline=True)
            except Exception:
                embed.add_field(name="Expira", value=str(case.get("expires_at")), inline=True)
        if case.get("evidence_url"):
            embed.add_field(name="Evidencia", value=str(case.get("evidence_url")), inline=False)
        embed.add_field(name="Razón", value=str(case.get("reason") or "—"), inline=False)
        return embed

    case_group = app_commands.Group(name="case", description="Gestión de casos de moderación")

    @case_group.command(name="update", description="Actualiza razón/evidencia/estado de un caso")
    @app_commands.describe(
        case_id="Número de caso o ID interno",
        razon="Nueva razón del caso",
        evidencia_url="Nueva URL de evidencia",
        estado="active, expired, revoked o failed",
    )
    async def case_update(
        self,
        interaction: discord.Interaction,
        case_id: int,
        razon: Optional[str] = None,
        evidencia_url: Optional[str] = None,
        estado: Optional[str] = None,
    ):
        if not self._has_mod_perms(interaction, "moderate_members"):
            return await interaction.response.send_message(_voice(self).line("error", "Este comando es solo para mods. El gato te miró feo."), ephemeral=True)
        case = self.db.get_case(interaction.guild_id, case_id)
        if not case:
            return await interaction.response.send_message(f"❌ No existe el caso `{case_id}`.", ephemeral=True)

        payload: Dict = {}
        if razon is not None:
            payload["reason"] = razon
        if evidencia_url is not None:
            payload["evidence_url"] = evidencia_url
        if estado is not None:
            normalized = estado.lower().strip()
            if normalized not in {"active", "expired", "revoked", "failed"}:
                return await interaction.response.send_message("❌ Estado inválido. Usa active, expired, revoked o failed.", ephemeral=True)
            payload["status"] = normalized
        if not payload:
            return await interaction.response.send_message("No hay cambios que aplicar.", ephemeral=True)

        updated = self.db.update_case_by_id(interaction.guild_id, int(case["id"]), **payload)
        self.db.log_action(
            interaction.guild_id, case["target_id"], interaction.user.id,
            "CASE_UPDATE", "Caso actualizado", {"case_id": case.get("id"), "changes": payload},
            parent_case_id=int(case["id"]),
            status=payload.get("status", case.get("status") or "active"),
        )
        await interaction.response.send_message(embed=self._case_embed(updated or case, title="Caso actualizado"), ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /clearwarns
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="clearwarns", description="Limpia todos los warns de un usuario")
    @app_commands.describe(usuario="Usuario al que limpiar los warns", razon="Razón del reseteo")
    async def clearwarns(
        self,
        interaction: discord.Interaction,
        usuario: discord.Member,
        razon: str = "Sin razón especificada",
    ):
        if not self._has_mod_perms(interaction, "administrator"):
            return await interaction.response.send_message(_voice(self).line("error", "Este comando es solo para mods. El gato te miró feo."), ephemeral=True)
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
            title="Warns limpiados",
            description=f"Se eliminaron **{old}** warn(s) de {usuario.mention}.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Usuario", value=usuario.mention, inline=True)
        embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="Razón", value=razon, inline=False)

        await interaction.response.send_message(embed=embed)
        await self._send_log(interaction.guild, embed)

    @clearwarns.error
    async def clearwarns_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await self._handle_perm_error(interaction, error)

    # ─────────────────────────────────────────────────────────────────────────
    # /purge
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="purgar", description="Elimina mensajes en masa del canal actual")
    @app_commands.describe(
        cantidad="Número de mensajes recientes a eliminar (2-100)",
        usuario="Eliminar solo mensajes de este usuario (opcional)",
        tipo="Filtro opcional: todos, links, invitaciones, bots o humano",
    )
    @app_commands.choices(tipo=[
        app_commands.Choice(name="Todos", value="all"),
        app_commands.Choice(name="Links", value="links"),
        app_commands.Choice(name="Invitaciones", value="invites"),
        app_commands.Choice(name="Bots", value="bots"),
        app_commands.Choice(name="Humanos", value="humans"),
    ])
    async def purge(
        self,
        interaction: discord.Interaction,
        cantidad: app_commands.Range[int, 2, 100],
        usuario: Optional[discord.Member] = None,
        tipo: str = "all",
    ):
        if not self._has_mod_perms(interaction, "manage_messages"):
            return await interaction.response.send_message(_voice(self).line("error", "Este comando es solo para mods. El gato te miró feo."), ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        tipo = (tipo or "all").lower().strip()

        def check(msg: discord.Message) -> bool:
            if usuario and msg.author.id != usuario.id:
                return False
            if tipo == "all":
                return True
            if tipo == "bots":
                return bool(msg.author.bot)
            if tipo == "humans":
                return not bool(msg.author.bot)
            content = msg.content or ""
            if tipo == "invites":
                return bool(INVITE_RE.search(content))
            if tipo == "links":
                return bool(URL_RE.search(content))
            return True

        try:
            deleted = await interaction.channel.purge(limit=cantidad, check=check, bulk=True)
        except discord.Forbidden:
            return await interaction.followup.send("❌ No tengo permisos para eliminar mensajes.", ephemeral=True)
        except discord.HTTPException as exc:
            return await interaction.followup.send(f"❌ Error al purgar: {exc}", ephemeral=True)

        self.db.log_action(
            interaction.guild_id,
            0,
            interaction.user.id,
            "PURGE",
            f"Purge {tipo}",
            {"deleted": len(deleted), "requested": cantidad, "filter": tipo, "user_id": usuario.id if usuario else None},
            status="expired",
        )

        embed = discord.Embed(
            title="Mensajes purgados",
            description=f"Se eliminaron **{len(deleted)}** mensajes en {interaction.channel.mention}.",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="Filtro", value=tipo, inline=True)
        if usuario:
            embed.add_field(name="Usuario filtrado", value=usuario.mention, inline=True)
        await self._send_log(interaction.guild, embed)

        await interaction.followup.send(
            f"✅ Eliminados **{len(deleted)}** mensajes.", ephemeral=True
        )

    # ─────────────────────────────────────────────────────────────────────────
    # /appeals
    # ─────────────────────────────────────────────────────────────────────────

    appeals_group = app_commands.Group(name="appeals", description="Gestión de apelaciones")

    @appeals_group.command(name="list", description="Lista las apelaciones pendientes del servidor")
    @app_commands.describe(
        estado="Filtrar por estado: PENDING (por defecto), ACCEPTED, DENIED",
    )
    async def appeals_list(
        self,
        interaction: discord.Interaction,
        estado: Optional[str] = None,
    ):
        if not self._has_mod_perms(interaction, "moderate_members"):
            return await interaction.response.send_message(_voice(self).line("error", "Este comando es solo para mods. El gato te miró feo."), ephemeral=True)

        status_filter = (estado or "PENDING").upper()
        if status_filter not in ("PENDING", "ACCEPTED", "DENIED"):
            return await interaction.response.send_message(
                "❌ Estado inválido. Usa: `PENDING`, `ACCEPTED` o `DENIED`.",
                ephemeral=True,
            )

        appeals = self.db.get_appeals_by_guild(interaction.guild_id, status_filter)

        if not appeals:
            return await interaction.response.send_message(
                f"📭 No hay apelaciones con estado **{status_filter}**.",
                ephemeral=True,
            )

        embed = discord.Embed(
            title=f"Apelaciones • {status_filter}",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Total: {len(appeals)}")

        for a in appeals[:10]:
            user = self.bot.get_user(a["user_id"])
            user_name = f"{user}" if user else f"`{a['user_id']}`"
            embed.add_field(
                name=f"#{a['id']} — {user_name}",
                value=(
                    f"**Sanción:** {a['action_type']}\n"
                    f"**Estado:** {a['status']}\n"
                    f"**Razón:** {a['reason'][:100]}\n"
                    f"**Creada:** <t:{int(datetime.fromisoformat(a['created_at']).timestamp())}:R>"
                ),
                inline=False,
            )

        if len(appeals) > 10:
            embed.description = f"Mostrando las 10 más recientes de {len(appeals)} totales."

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /modconfig — configuración crítica también disponible por Discord
    # ─────────────────────────────────────────────────────────────────────────

    modconfig_group = app_commands.Group(
        name="modconfig",
        description="Configura roles y canales críticos de moderación",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @modconfig_group.command(name="status", description="Muestra la configuración actual de moderación")
    async def modconfig_status(self, interaction: discord.Interaction):
        if not self._has_config_perms(interaction):
            return await interaction.response.send_message("❌ Necesitas Administrador o Gestionar servidor.", ephemeral=True)
        cfg = self.db.get_config(interaction.guild_id)
        srv = self.db.get_server_config(interaction.guild_id)
        embed = discord.Embed(
            title="Configuración de moderación",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Rol mute", value=f"<@&{cfg.get('mute_role_id')}>" if cfg.get("mute_role_id") else "—", inline=True)
        embed.add_field(name="Rol mod", value=f"<@&{srv.get('mod_role_id')}>" if srv.get("mod_role_id") else "—", inline=True)
        staff_id = srv.get("staff_role_id") or cfg.get("staff_role_id")
        embed.add_field(name="Rol staff", value=f"<@&{staff_id}>" if staff_id else "—", inline=True)
        embed.add_field(name="Modlog", value=f"<#{srv.get('modlog_channel')}>" if srv.get("modlog_channel") else "—", inline=True)
        embed.add_field(name="Serverlog", value=f"<#{srv.get('serverlog_channel')}>" if srv.get("serverlog_channel") else "—", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @modconfig_group.command(name="mute_role", description="Asigna el rol que usará /mute")
    @app_commands.describe(rol="Rol que se aplicará a usuarios muteados")
    async def modconfig_mute_role(self, interaction: discord.Interaction, rol: discord.Role):
        if not self._has_config_perms(interaction):
            return await interaction.response.send_message("❌ Necesitas Administrador o Gestionar servidor.", ephemeral=True)
        bot_member = interaction.guild.me if interaction.guild else None
        if bot_member and rol >= bot_member.top_role:
            return await interaction.response.send_message(
                "❌ Ese rol está por encima o al mismo nivel que mi rol. Muévelo debajo de mi rol antes de usarlo como mute.",
                ephemeral=True,
            )
        self.db.set_config(interaction.guild_id, mute_role_id=rol.id)
        changed = await self._apply_mute_overwrites(interaction.guild, rol)
        await interaction.response.send_message(
            f"✅ Rol de mute configurado: {rol.mention}. Overrides aplicados en `{changed}` canales visibles.",
            ephemeral=True,
        )

    @modconfig_group.command(name="mod_role", description="Asigna el rol con permisos de moderación del bot")
    @app_commands.describe(rol="Rol de moderador")
    async def modconfig_mod_role(self, interaction: discord.Interaction, rol: discord.Role):
        if not self._has_config_perms(interaction):
            return await interaction.response.send_message("❌ Necesitas Administrador o Gestionar servidor.", ephemeral=True)
        self.db.set_server_config(interaction.guild_id, mod_role_id=rol.id)
        await interaction.response.send_message(f"✅ Rol moderador configurado: {rol.mention}", ephemeral=True)

    @modconfig_group.command(name="staff_role", description="Asigna el rol staff usado por moderación/tickets/reportes")
    @app_commands.describe(rol="Rol staff")
    async def modconfig_staff_role(self, interaction: discord.Interaction, rol: discord.Role):
        if not self._has_config_perms(interaction):
            return await interaction.response.send_message("❌ Necesitas Administrador o Gestionar servidor.", ephemeral=True)
        self.db.set_config(interaction.guild_id, staff_role_id=rol.id)
        self.db.set_server_config(interaction.guild_id, staff_role_id=rol.id)
        await interaction.response.send_message(f"✅ Rol staff configurado: {rol.mention}", ephemeral=True)

    @modconfig_group.command(name="modlog", description="Asigna el canal de logs de moderación")
    @app_commands.describe(canal="Canal de texto para logs de moderación")
    async def modconfig_modlog(self, interaction: discord.Interaction, canal: discord.TextChannel):
        if not self._has_config_perms(interaction):
            return await interaction.response.send_message("❌ Necesitas Administrador o Gestionar servidor.", ephemeral=True)
        self.db.set_server_config(interaction.guild_id, modlog_channel=canal.id, modlog_enabled=1)
        await interaction.response.send_message(f"✅ Canal de modlogs configurado: {canal.mention}", ephemeral=True)

    @modconfig_group.command(name="serverlog", description="Asigna el canal de logs de eventos del servidor")
    @app_commands.describe(canal="Canal de texto para logs del servidor")
    async def modconfig_serverlog(self, interaction: discord.Interaction, canal: discord.TextChannel):
        if not self._has_config_perms(interaction):
            return await interaction.response.send_message("❌ Necesitas Administrador o Gestionar servidor.", ephemeral=True)
        self.db.set_server_config(interaction.guild_id, serverlog_channel=canal.id, serverlog_enabled=1)
        await interaction.response.send_message(f"✅ Canal de serverlogs configurado: {canal.mention}", ephemeral=True)

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

        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except discord.NotFound:
            logger.warning("No se pudo responder error de moderación: interacción expirada o desconocida")
        except discord.HTTPException as exc:
            logger.warning("No se pudo responder error de moderación: %s", exc)


# ── Views y Modals para /modconfig ────────────────────────────────────────────
# ELIMINADOS: ModConfigView, MuteRoleSelectView, MuteDurationConfigModal,
# ThresholdsConfigModal, ConsequencesToggleView
# → Configuración migrada al Dashboard Web


# ── Appeals UI ────────────────────────────────────────────────────────────────

class AppealUserModal(discord.ui.Modal, title="Apelar Sanción"):
    appeal_text = discord.ui.TextInput(
        label="¿Por qué deberíamos retirar tu sanción?",
        style=discord.TextStyle.paragraph,
        placeholder="Explica tu situación detalladamente...",
        required=True,
        max_length=1000
    )

    def __init__(self, bot: commands.Bot, guild_id: int, action_type: str, reason: str):
        super().__init__()
        self.bot = bot
        self.guild_id = guild_id
        self.action_type = action_type
        self.reason = reason

    async def on_submit(self, interaction: discord.Interaction):
        db = getattr(self.bot, 'db')
        appeal_id = db.create_appeal(
            self.guild_id, interaction.user.id, self.action_type, self.reason, self.appeal_text.value
        )

        guild = self.bot.get_guild(self.guild_id)
        if not guild:
            await interaction.response.send_message(
                "⚠️ Tu apelación quedó registrada pero el bot no puede ver el servidor en este momento.",
                ephemeral=True,
            )
            logger.warning("No se pudo publicar apelación %s: guild %s no disponible", appeal_id, self.guild_id)
            return

        embed = discord.Embed(
            title="Nueva Apelación Recibida",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Usuario", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
        embed.add_field(name="Sanción", value=self.action_type, inline=True)
        embed.add_field(name="Razón Original", value=self.reason, inline=True)
        embed.add_field(name="Defensa del Usuario", value=self.appeal_text.value, inline=False)
        embed.set_footer(text=f"ID Apelación: {appeal_id}")

        cog = self.bot.get_cog("Moderation")
        modlog = await cog._resolve_modlog(guild) if cog else None

        if modlog is None:
            await interaction.response.send_message(
                "✅ Tu apelación quedó registrada. El equipo la revisará desde el dashboard.\n"
                "ℹ️ Aviso al staff: el canal de mod-logs no está configurado o no es accesible.",
                ephemeral=True,
            )
            logger.warning(
                "Apelación %s guardada pero no publicada: modlog inaccesible en guild %s",
                appeal_id, self.guild_id,
            )
            return

        try:
            await modlog.send(
                embed=embed,
                view=AppealModView(self.bot, appeal_id, interaction.user.id, self.action_type),
            )
            await interaction.response.send_message(
                "✅ Tu apelación ha sido enviada al equipo de moderación. Recibirás un DM con la respuesta.",
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "⚠️ Tu apelación quedó registrada pero el bot no pudo publicarla en el canal de mod-logs (sin permisos).",
                ephemeral=True,
            )
            logger.warning("Sin permisos para publicar apelación %s en modlog", appeal_id)
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                "⚠️ Tu apelación quedó registrada pero hubo un error publicándola en mod-logs.",
                ephemeral=True,
            )
            logger.warning("Error enviando apelación %s a modlog: %s", appeal_id, exc)


class AppealUserView(discord.ui.View):
    def __init__(self, bot: commands.Bot, guild_id: int, action_type: str, reason: str):
        super().__init__(timeout=86400)
        self.bot = bot
        self.guild_id = guild_id
        self.action_type = action_type
        self.reason = reason

    @discord.ui.button(label="Apelar Sanción", style=discord.ButtonStyle.primary, emoji="📝")
    async def appeal_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AppealUserModal(self.bot, self.guild_id, self.action_type, self.reason))
        button.disabled = True
        if interaction.message:
            try:
                await interaction.message.edit(view=self)
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.debug("No se pudo deshabilitar botón de apelación: %s", exc)


class AppealAcceptModal(discord.ui.Modal, title="Aceptar Apelación"):
    mod_reason = discord.ui.TextInput(
        label="Mensaje para el usuario",
        style=discord.TextStyle.paragraph,
        placeholder="Ej: Se retirará tu sanción porque...",
        required=True
    )
    auto_remove = discord.ui.TextInput(
        label="¿Quitar sanción automáticamente? (SI/NO)",
        style=discord.TextStyle.short,
        default="SI",
        required=True
    )

    def __init__(self, bot: commands.Bot, appeal_id: int, user_id: int, action_type: str):
        super().__init__()
        self.bot = bot
        self.appeal_id = appeal_id
        self.user_id = user_id
        self.action_type = action_type

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        db = getattr(self.bot, 'db')
        db.update_appeal_status(self.appeal_id, "ACCEPTED")
        guild = interaction.guild
        member = guild.get_member(self.user_id) if guild else None
        recipient = member or self.bot.get_user(self.user_id)

        auto_text = self.auto_remove.value.strip().upper()
        if auto_text == "SI" and guild:
            try:
                if self.action_type in ("BAN", "TEMPBAN"):
                    await guild.unban(discord.Object(id=self.user_id), reason=f"Apelación Aceptada por {interaction.user}")
                    db.clear_tempbans_for_user(self.user_id, guild.id)
                elif self.action_type == "MUTE":
                    mem = guild.get_member(self.user_id)
                    cfg = db.get_config(guild.id)
                    mute_role = guild.get_role(cfg.get("mute_role_id") or 0)
                    if mem and mute_role and mute_role in mem.roles:
                        await mem.remove_roles(mute_role, reason=f"Apelación Aceptada por {interaction.user}")
                    db.clear_mute(self.user_id, guild.id)
            except discord.Forbidden:
                logger.warning("Sin permisos para retirar sanción automáticamente en apelación %s", self.appeal_id)
            except discord.HTTPException as exc:
                logger.warning("Error quitando sanción automáticamente en apelación %s: %s", self.appeal_id, exc)

        if recipient and guild:
            embed = discord.Embed(
                title="✅ Apelación Aceptada",
                description=f"Tu apelación en **{guild.name}** ha sido aceptada.",
                color=discord.Color.green()
            )
            embed.add_field(name="Sanción Original", value=self.action_type)
            embed.add_field(name="Mensaje del Moderador", value=self.mod_reason.value, inline=False)
            try:
                await recipient.send(embed=embed)
            except discord.Forbidden:
                logger.info("No se pudo notificar por DM la apelación aceptada a %s", self.user_id)
            except discord.HTTPException as exc:
                logger.warning("Error notificando apelación aceptada a %s: %s", self.user_id, exc)

        if interaction.message and interaction.message.embeds:
            embed = interaction.message.embeds[0].copy()
            embed.color = discord.Color.green()
            embed.title = "✅ Apelación Aceptada"
            embed.add_field(name="Moderador", value=interaction.user.mention, inline=False)
            embed.add_field(name="Motivo de Aceptación", value=self.mod_reason.value, inline=False)
            await interaction.message.edit(embed=embed, view=None)

        await interaction.followup.send("Apelación aceptada.", ephemeral=True)


class AppealDenyModal(discord.ui.Modal, title="Denegar Apelación"):
    mod_reason = discord.ui.TextInput(
        label="Mensaje para el usuario",
        style=discord.TextStyle.paragraph,
        placeholder="Ej: Tu apelación ha sido denegada porque...",
        required=True
    )

    def __init__(self, bot: commands.Bot, appeal_id: int, user_id: int, action_type: str):
        super().__init__()
        self.bot = bot
        self.appeal_id = appeal_id
        self.user_id = user_id
        self.action_type = action_type

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        db = getattr(self.bot, 'db')
        db.update_appeal_status(self.appeal_id, "DENIED")
        guild = interaction.guild
        member = guild.get_member(self.user_id) if guild else None
        recipient = member or self.bot.get_user(self.user_id)

        if recipient and guild:
            embed = discord.Embed(
                title="❌ Apelación Denegada",
                description=f"Tu apelación en **{guild.name}** ha sido denegada.",
                color=discord.Color.red()
            )
            embed.add_field(name="Sanción Original", value=self.action_type)
            embed.add_field(name="Mensaje del Moderador", value=self.mod_reason.value, inline=False)
            try:
                await recipient.send(embed=embed)
            except discord.Forbidden:
                logger.info("No se pudo notificar por DM la apelación denegada a %s", self.user_id)
            except discord.HTTPException as exc:
                logger.warning("Error notificando apelación denegada a %s: %s", self.user_id, exc)

        if interaction.message and interaction.message.embeds:
            embed = interaction.message.embeds[0].copy()
            embed.color = discord.Color.red()
            embed.title = "❌ Apelación Denegada"
            embed.add_field(name="Moderador", value=interaction.user.mention, inline=False)
            embed.add_field(name="Motivo de Denegación", value=self.mod_reason.value, inline=False)
            await interaction.message.edit(embed=embed, view=None)

        await interaction.followup.send("Apelación denegada.", ephemeral=True)


class AppealModView(discord.ui.View):
    def __init__(self, bot: commands.Bot, appeal_id: int = 0, user_id: int = 0, action_type: str = "UNKNOWN"):
        super().__init__(timeout=None)
        self.bot = bot
        self.appeal_id = appeal_id
        self.user_id = user_id
        self.action_type = action_type

    @staticmethod
    def _parse_embed(interaction: discord.Interaction) -> tuple:
        """Extrae (appeal_id, user_id, action_type) del embed del mensaje."""
        appeal_id, user_id, action_type = 0, 0, "UNKNOWN"
        if not (interaction.message and interaction.message.embeds):
            return appeal_id, user_id, action_type
        embed = interaction.message.embeds[0]
        # Footer: "ID Apelación: N"
        if embed.footer and embed.footer.text:
            try:
                appeal_id = int(embed.footer.text.split("ID Apelación:")[-1].strip())
            except (ValueError, IndexError):
                pass
        for field in embed.fields:
            if field.name == "Sanción":
                action_type = field.value or "UNKNOWN"
            elif field.name == "Usuario" and field.value:
                # Formato: "mención (`ID`)"
                try:
                    user_id = int(field.value.split("`")[1])
                except (IndexError, ValueError):
                    pass
        return appeal_id, user_id, action_type

    @discord.ui.button(label="Aceptar", style=discord.ButtonStyle.success, emoji="✅", custom_id="appeal_accept")
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        appeal_id, user_id, action_type = self._parse_embed(interaction)
        if not appeal_id:
            appeal_id, user_id, action_type = self.appeal_id, self.user_id, self.action_type
        await interaction.response.send_modal(AppealAcceptModal(self.bot, appeal_id, user_id, action_type))

    @discord.ui.button(label="Denegar", style=discord.ButtonStyle.danger, emoji="❌", custom_id="appeal_deny")
    async def deny_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        appeal_id, user_id, action_type = self._parse_embed(interaction)
        if not appeal_id:
            appeal_id, user_id, action_type = self.appeal_id, self.user_id, self.action_type
        await interaction.response.send_modal(AppealDenyModal(self.bot, appeal_id, user_id, action_type))



async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
    # Registrar AppealModView como vista persistente (botones con custom_id fijos)
    # Los datos del appeal se extraen del embed al interactuar
    bot.add_view(AppealModView(bot))
