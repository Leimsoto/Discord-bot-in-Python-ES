"""
cogs/serverutils.py
───────────────────
Utilidades de servidor: información, configuración global y logs en tiempo real.

Comandos slash:
  /serverinfo  – Información detallada del servidor
  /config      – Panel de configuración global del bot (roles por módulo)
  /serverlogs  – Configuración de captura de logs en tiempo real
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("ServerUtils")

DEFAULT_LOG_EVENTS = {
    "message_delete": True,
    "message_edit": True,
    "member_join": True,
    "member_leave": True,
    "voice_join_leave": True,
    "role_changes": True,
    "nickname_changes": True,
    "message_send": False,
    "reactions": False,
}


class ServerUtils(commands.Cog):
    """Información del servidor, configuración global y sistema de logs."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db  # type: ignore

    def _get_log_events(self, guild_id: int) -> dict:
        cfg = self.db.get_server_config(guild_id)
        if cfg.get("log_events"):
            try:
                return json.loads(cfg["log_events"])
            except json.JSONDecodeError:
                pass
        return dict(DEFAULT_LOG_EVENTS)

    async def _send_server_log(self, guild: discord.Guild, embed: discord.Embed) -> None:
        cfg = self.db.get_server_config(guild.id)
        ch_id = cfg.get("serverlog_channel")
        if not ch_id:
            return
        channel = guild.get_channel(ch_id)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                pass

    # ─────────────────────────────────────────────────────────────────────
    # /serverinfo
    # ─────────────────────────────────────────────────────────────────────

    @app_commands.command(name="serverinfo", description="Información detallada del servidor")
    async def serverinfo(self, interaction: discord.Interaction):
        g = interaction.guild
        await interaction.response.defer()

        created = int(g.created_at.timestamp())
        online = sum(1 for m in g.members if m.status != discord.Status.offline)
        bots = sum(1 for m in g.members if m.bot)
        text_ch = len(g.text_channels)
        voice_ch = len(g.voice_channels)
        cats = len(g.categories)

        embed = discord.Embed(
            title=f"🏠 {g.name}",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)
        if g.banner:
            embed.set_image(url=g.banner.url)

        embed.add_field(name="🆔 ID", value=f"`{g.id}`", inline=True)
        embed.add_field(name="👑 Dueño", value=f"{g.owner.mention}" if g.owner else "—", inline=True)
        embed.add_field(name="📅 Creado", value=f"<t:{created}:R>", inline=True)
        embed.add_field(
            name=f"👥 Miembros ({g.member_count})",
            value=f"🟢 {online} en línea · 🤖 {bots} bots",
            inline=False,
        )
        embed.add_field(
            name=f"📺 Canales ({text_ch + voice_ch})",
            value=f"💬 {text_ch} texto · 🔊 {voice_ch} voz · 📁 {cats} categorías",
            inline=False,
        )
        embed.add_field(name="🏷️ Roles", value=f"`{len(g.roles) - 1}`", inline=True)
        embed.add_field(name="😀 Emojis", value=f"`{len(g.emojis)}`", inline=True)
        embed.add_field(name="🔒 Verificación", value=f"`{g.verification_level}`", inline=True)

        if g.premium_subscription_count:
            embed.add_field(
                name="💎 Boosts",
                value=f"`{g.premium_subscription_count}` (Nivel {g.premium_tier})",
                inline=True,
            )

        features = g.features[:8] if g.features else []
        if features:
            embed.add_field(
                name="✨ Características",
                value=", ".join(f.replace("_", " ").title() for f in features),
                inline=False,
            )

        embed.set_footer(text=f"Servidor ID: {g.id}")
        await interaction.followup.send(embed=embed)

    # ─────────────────────────────────────────────────────────────────────
    # /config — Panel de configuración global
    # ─────────────────────────────────────────────────────────────────────

    @app_commands.command(name="config", description="Panel de configuración global del bot")
    @app_commands.checks.has_permissions(administrator=True)
    async def config(self, interaction: discord.Interaction):
        srv = self.db.get_server_config(interaction.guild_id)
        embed = self._build_config_embed(interaction.guild, srv)
        view = GlobalConfigView(self, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    def _build_config_embed(self, guild: discord.Guild, srv: dict) -> discord.Embed:
        def role_or_none(rid):
            if not rid:
                return "❌ No configurado"
            r = guild.get_role(rid)
            return r.mention if r else "❌ Rol eliminado"

        def ch_or_none(cid):
            if not cid:
                return "❌ No configurado"
            c = guild.get_channel(cid)
            return c.mention if c else "❌ Canal eliminado"

        embed = discord.Embed(
            title="🤖 Configuración Global del Bot",
            description=f"Servidor: **{guild.name}**",
            color=discord.Color.dark_teal(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👮 Rol Staff (global)", value=role_or_none(srv.get("staff_role_id")), inline=True)
        embed.add_field(name="🎨 Rol Embeds", value=role_or_none(srv.get("embed_role_id")), inline=True)
        embed.add_field(name="📺 Rol Canales", value=role_or_none(srv.get("channels_role_id")), inline=True)
        embed.add_field(name="👥 Rol Usuarios", value=role_or_none(srv.get("users_role_id")), inline=True)
        embed.add_field(name="📋 Canal Mod-Logs", value=ch_or_none(srv.get("modlog_channel")), inline=True)
        embed.add_field(name="📡 Canal Server-Logs", value=ch_or_none(srv.get("serverlog_channel")), inline=True)
        embed.set_footer(text="Usa los botones para modificar")
        return embed

    @config.error
    async def config_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ Solo administradores.", ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────
    # /serverlogs — Configuración de eventos
    # ─────────────────────────────────────────────────────────────────────

    @app_commands.command(name="serverlogs", description="Configura los logs del servidor en tiempo real")
    @app_commands.checks.has_permissions(administrator=True)
    async def serverlogs(self, interaction: discord.Interaction):
        events = self._get_log_events(interaction.guild_id)
        srv = self.db.get_server_config(interaction.guild_id)
        embed = self._build_logs_embed(interaction.guild, srv, events)
        view = ServerLogsView(self, interaction.user.id, events)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    def _build_logs_embed(self, guild, srv, events):
        ch_id = srv.get("serverlog_channel")
        ch = guild.get_channel(ch_id) if ch_id else None

        embed = discord.Embed(
            title="📡 Configuración de Server Logs",
            description=f"Canal: {ch.mention if ch else '❌ No configurado'}",
            color=discord.Color.dark_gold(),
            timestamp=datetime.now(timezone.utc),
        )

        event_labels = {
            "message_delete": "🗑️ Mensajes eliminados",
            "message_edit": "✏️ Mensajes editados",
            "member_join": "📥 Miembros: unión",
            "member_leave": "📤 Miembros: salida",
            "voice_join_leave": "🔊 Voz: unión/salida",
            "role_changes": "🏷️ Cambios de roles",
            "nickname_changes": "📛 Cambios de nickname",
            "message_send": "💬 Cada mensaje enviado",
            "reactions": "😀 Reacciones",
        }
        lines = []
        for key, label in event_labels.items():
            icon = "✅" if events.get(key, False) else "❌"
            lines.append(f"{icon} {label}")
        embed.add_field(name="Eventos activos", value="\n".join(lines), inline=False)
        embed.set_footer(text="Usa los botones para cambiar eventos")
        return embed

    # ─────────────────────────────────────────────────────────────────────
    # Listeners de logs en tiempo real
    # ─────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        events = self._get_log_events(message.guild.id)
        if not events.get("message_delete"):
            return
        embed = discord.Embed(
            title="🗑️ Mensaje eliminado",
            description=f"**Canal:** {message.channel.mention}\n**Autor:** {message.author.mention}",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        content = message.content or "[Sin contenido de texto]"
        if len(content) > 1024:
            content = content[:1021] + "..."
        embed.add_field(name="Contenido", value=content, inline=False)
        embed.set_footer(text=f"ID Mensaje: {message.id} | ID Usuario: {message.author.id}")
        await self._send_server_log(message.guild, embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not before.guild or before.author.bot or before.content == after.content:
            return
        events = self._get_log_events(before.guild.id)
        if not events.get("message_edit"):
            return
        embed = discord.Embed(
            title="✏️ Mensaje editado",
            description=f"**Canal:** {before.channel.mention}\n**Autor:** {before.author.mention}\n[Ir al mensaje]({after.jump_url})",
            color=discord.Color.yellow(),
            timestamp=datetime.now(timezone.utc),
        )
        old = (before.content or "—")[:500]
        new = (after.content or "—")[:500]
        embed.add_field(name="Antes", value=old, inline=False)
        embed.add_field(name="Después", value=new, inline=False)
        embed.set_footer(text=f"ID: {before.id}")
        await self._send_server_log(before.guild, embed)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        events = self._get_log_events(member.guild.id)
        if not events.get("member_join"):
            return
        created = int(member.created_at.timestamp())
        embed = discord.Embed(
            title="📥 Miembro se unió",
            description=f"{member.mention} (`{member}`)",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Cuenta creada", value=f"<t:{created}:R>", inline=True)
        embed.add_field(name="Miembros totales", value=f"`{member.guild.member_count}`", inline=True)
        embed.set_footer(text=f"ID: {member.id}")
        await self._send_server_log(member.guild, embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        events = self._get_log_events(member.guild.id)
        if not events.get("member_leave"):
            return
        roles = [r.mention for r in member.roles if r != member.guild.default_role]
        embed = discord.Embed(
            title="📤 Miembro salió",
            description=f"{member.mention} (`{member}`)",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        if roles:
            embed.add_field(name="Roles", value=", ".join(roles[:10]), inline=False)
        embed.set_footer(text=f"ID: {member.id}")
        await self._send_server_log(member.guild, embed)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        events = self._get_log_events(member.guild.id)
        if not events.get("voice_join_leave"):
            return
        if before.channel == after.channel:
            return
        if after.channel and not before.channel:
            embed = discord.Embed(
                title="🔊 Unión a canal de voz",
                description=f"{member.mention} se unió a **{after.channel.name}**",
                color=discord.Color.green(), timestamp=datetime.now(timezone.utc),
            )
        elif before.channel and not after.channel:
            embed = discord.Embed(
                title="🔇 Salida de canal de voz",
                description=f"{member.mention} salió de **{before.channel.name}**",
                color=discord.Color.orange(), timestamp=datetime.now(timezone.utc),
            )
        else:
            embed = discord.Embed(
                title="🔀 Cambio de canal de voz",
                description=f"{member.mention}: **{before.channel.name}** → **{after.channel.name}**",
                color=discord.Color.blue(), timestamp=datetime.now(timezone.utc),
            )
        embed.set_footer(text=f"ID: {member.id}")
        await self._send_server_log(member.guild, embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        events = self._get_log_events(before.guild.id)

        # Cambio de roles
        if events.get("role_changes") and before.roles != after.roles:
            added = set(after.roles) - set(before.roles)
            removed = set(before.roles) - set(after.roles)
            if added or removed:
                embed = discord.Embed(
                    title="🏷️ Cambio de roles",
                    description=f"**Usuario:** {after.mention}",
                    color=discord.Color.purple(), timestamp=datetime.now(timezone.utc),
                )
                if added:
                    embed.add_field(name="➕ Añadidos", value=", ".join(r.mention for r in added), inline=False)
                if removed:
                    embed.add_field(name="➖ Removidos", value=", ".join(r.mention for r in removed), inline=False)
                embed.set_footer(text=f"ID: {after.id}")
                await self._send_server_log(before.guild, embed)

        # Cambio de nickname
        if events.get("nickname_changes") and before.nick != after.nick:
            embed = discord.Embed(
                title="📛 Cambio de nickname",
                description=f"**Usuario:** {after.mention}",
                color=discord.Color.teal(), timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Antes", value=before.nick or before.name, inline=True)
            embed.add_field(name="Después", value=after.nick or after.name, inline=True)
            embed.set_footer(text=f"ID: {after.id}")
            await self._send_server_log(before.guild, embed)


# ── Views para /config ────────────────────────────────────────────────────────

class GlobalConfigView(discord.ui.View):
    def __init__(self, cog: ServerUtils, author_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Solo quien abrió el panel.", ephemeral=True)
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction):
        srv = self.cog.db.get_server_config(interaction.guild_id)
        embed = self.cog._build_config_embed(interaction.guild, srv)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Rol Staff", emoji="👮", style=discord.ButtonStyle.primary, row=0)
    async def staff_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(view=RoleSelectView(self, "staff_role_id", "Rol Staff global"))

    @discord.ui.button(label="Rol Embeds", emoji="🎨", style=discord.ButtonStyle.primary, row=0)
    async def embed_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(view=RoleSelectView(self, "embed_role_id", "Rol Embeds"))

    @discord.ui.button(label="Rol Canales", emoji="📺", style=discord.ButtonStyle.secondary, row=1)
    async def channels_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(view=RoleSelectView(self, "channels_role_id", "Rol Canales"))

    @discord.ui.button(label="Rol Usuarios", emoji="👥", style=discord.ButtonStyle.secondary, row=1)
    async def users_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(view=RoleSelectView(self, "users_role_id", "Rol Usuarios"))

    @discord.ui.button(label="Canal Mod-Logs", emoji="📋", style=discord.ButtonStyle.secondary, row=2)
    async def modlog_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(view=ChannelSelectConfigView(self, "modlog_channel"))

    @discord.ui.button(label="Canal Server-Logs", emoji="📡", style=discord.ButtonStyle.secondary, row=2)
    async def serverlog_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(view=ChannelSelectConfigView(self, "serverlog_channel"))

    @discord.ui.button(label="Cerrar", emoji="❌", style=discord.ButtonStyle.danger, row=3)
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="✅ Panel cerrado.", embed=None, view=None)
        self.stop()


class RoleSelectView(discord.ui.View):
    def __init__(self, parent: GlobalConfigView, config_key: str, label: str):
        super().__init__(timeout=60)
        self.parent = parent
        self.config_key = config_key
        self.label = label

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Selecciona un rol", min_values=1, max_values=1)
    async def select_role(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0]
        self.parent.cog.db.set_server_config(interaction.guild_id, **{self.config_key: role.id})
        await self.parent._refresh(interaction)

    @discord.ui.button(label="Volver", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.parent._refresh(interaction)


class ChannelSelectConfigView(discord.ui.View):
    def __init__(self, parent: GlobalConfigView, config_key: str):
        super().__init__(timeout=60)
        self.parent = parent
        self.config_key = config_key

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="Selecciona un canal",
                       channel_types=[discord.ChannelType.text], min_values=1, max_values=1)
    async def select_ch(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        ch = select.values[0]
        self.parent.cog.db.set_server_config(interaction.guild_id, **{self.config_key: ch.id})
        await self.parent._refresh(interaction)

    @discord.ui.button(label="Volver", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.parent._refresh(interaction)


# ── View para /serverlogs ─────────────────────────────────────────────────────

class ServerLogsView(discord.ui.View):
    def __init__(self, cog: ServerUtils, author_id: int, events: dict):
        super().__init__(timeout=300)
        self.cog = cog
        self.author_id = author_id
        self.events = events
        self._add_toggles()

    def _add_toggles(self):
        """Crea un StringSelect con todos los eventos como opciones."""
        labels = {
            "message_delete": "🗑️ Mensajes eliminados",
            "message_edit": "✏️ Mensajes editados",
            "member_join": "📥 Miembros: unión",
            "member_leave": "📤 Miembros: salida",
            "voice_join_leave": "🔊 Voz: unión/salida",
            "role_changes": "🏷️ Cambios de roles",
            "nickname_changes": "📛 Cambios de nickname",
            "message_send": "💬 Cada mensaje enviado",
            "reactions": "😀 Reacciones",
        }
        options = []
        for key, label in labels.items():
            is_on = self.events.get(key, False)
            options.append(discord.SelectOption(
                label=label, value=key,
                description="Activado" if is_on else "Desactivado",
                default=is_on,
            ))
        select = discord.ui.Select(
            placeholder="Selecciona los eventos a activar",
            min_values=0, max_values=len(options), options=options,
        )
        select.callback = self._toggle_callback
        self.add_item(select)

    async def _toggle_callback(self, interaction: discord.Interaction):
        selected = set(interaction.data.get("values", []))
        for key in self.events:
            self.events[key] = key in selected
        self.cog.db.set_server_config(
            interaction.guild_id,
            log_events=json.dumps(self.events),
        )
        srv = self.cog.db.get_server_config(interaction.guild_id)
        embed = self.cog._build_logs_embed(interaction.guild, srv, self.events)

        self.clear_items()
        self._add_toggles()
        close_btn = discord.ui.Button(label="Cerrar", emoji="❌", style=discord.ButtonStyle.danger)
        close_btn.callback = self._close
        self.add_item(close_btn)

        await interaction.response.edit_message(embed=embed, view=self)

    async def _close(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="✅ Panel cerrado.", embed=None, view=None)
        self.stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Solo quien abrió el panel.", ephemeral=True)
            return False
        return True


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerUtils(bot))
