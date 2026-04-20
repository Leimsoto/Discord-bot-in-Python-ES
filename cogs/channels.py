"""
cogs/channels.py
────────────────
Gestión de canales.

Comandos slash:
  /lock         – Bloquear canal (deniega envío a @everyone)
  /unlock       – Desbloquear canal
  /clear        – Eliminar N mensajes (1-100)
  /clearall     – Eliminar TODOS los mensajes (con confirmación)
  /slowmode     – Configurar modo lento
  /channelsetup – Panel interactivo: multimedia-only y autorreacción

Requiere permiso: manage_messages o administrator.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("Channels")


# ── Helper: verificar rol de módulo ───────────────────────────────────────────

async def check_channel_perms(interaction: discord.Interaction) -> bool:
    """Verifica que el usuario tenga permisos de gestión de canales."""
    member = interaction.user
    if member.guild_permissions.administrator or member.guild_permissions.manage_messages:
        return True
    # Verificar rol de módulo
    srv_cfg = interaction.client.db.get_server_config(interaction.guild_id)
    role_id = srv_cfg.get("channels_role_id")
    if role_id and any(r.id == role_id for r in member.roles):
        return True
    await interaction.response.send_message(
        "❌ Necesitas el permiso **Gestionar Mensajes**, ser administrador, "
        "o tener el rol de canales configurado.", ephemeral=True,
    )
    return False


class Channels(commands.Cog):
    """Gestión de canales: bloqueo, limpieza, slowmode, multimedia y autorreacción."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db  # type: ignore

    # ─────────────────────────────────────────────────────────────────────────
    # /lock
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="lock", description="Bloquea el canal — nadie puede enviar mensajes")
    @app_commands.describe(razon="Razón del bloqueo")
    async def lock(
        self, interaction: discord.Interaction,
        razon: str = "Canal bloqueado por un moderador",
    ):
        if not await check_channel_perms(interaction):
            return

        channel = interaction.channel
        overwrites = channel.overwrites_for(interaction.guild.default_role)
        if overwrites.send_messages is False:
            return await interaction.response.send_message(
                "⚠️ Este canal ya está bloqueado.", ephemeral=True
            )

        await channel.set_permissions(
            interaction.guild.default_role,
            send_messages=False,
            reason=f"Lock: {razon} | Mod: {interaction.user}",
        )
        self.db.set_channel_config(channel.id, interaction.guild_id, locked=1)

        embed = discord.Embed(
            title="🔒 Canal Bloqueado",
            description=f"Este canal ha sido bloqueado.\n**Razón:** {razon}",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Bloqueado por {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)

    # ─────────────────────────────────────────────────────────────────────────
    # /unlock
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="unlock", description="Desbloquea el canal")
    async def unlock(self, interaction: discord.Interaction):
        if not await check_channel_perms(interaction):
            return

        channel = interaction.channel
        overwrites = channel.overwrites_for(interaction.guild.default_role)
        if overwrites.send_messages is not False:
            return await interaction.response.send_message(
                "⚠️ Este canal no está bloqueado.", ephemeral=True
            )

        await channel.set_permissions(
            interaction.guild.default_role,
            send_messages=None,
            reason=f"Unlock | Mod: {interaction.user}",
        )
        self.db.set_channel_config(channel.id, interaction.guild_id, locked=0)

        embed = discord.Embed(
            title="🔓 Canal Desbloqueado",
            description="Este canal ha sido desbloqueado.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Desbloqueado por {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)

    # ─────────────────────────────────────────────────────────────────────────
    # /clear
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="clear", description="Elimina una cantidad de mensajes del canal")
    @app_commands.describe(cantidad="Cantidad de mensajes a eliminar (1-100)")
    async def clear(
        self, interaction: discord.Interaction,
        cantidad: app_commands.Range[int, 1, 100],
    ):
        if not await check_channel_perms(interaction):
            return

        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=cantidad)

        embed = discord.Embed(
            title="🧹 Mensajes eliminados",
            description=f"Se eliminaron **{len(deleted)}** mensaje(s).",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Ejecutado por {interaction.user.display_name}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /clearall
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="clearall", description="Elimina TODOS los mensajes del canal (irreversible)")
    async def clearall(self, interaction: discord.Interaction):
        if not await check_channel_perms(interaction):
            return

        embed = discord.Embed(
            title="⚠️ ADVERTENCIA — Limpieza Total",
            description=(
                "Esto **eliminará TODOS los mensajes** de este canal.\n"
                "La acción es **IRREVERSIBLE** y clonará el canal.\n\n"
                "**¿Estás absolutamente seguro?**"
            ),
            color=discord.Color.dark_red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="Tienes 30 segundos para confirmar")

        view = ClearAllConfirmView(interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /slowmode
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="slowmode", description="Configura el modo lento del canal")
    @app_commands.describe(segundos="Segundos entre mensajes (0 para desactivar, máx 21600)")
    async def slowmode(
        self, interaction: discord.Interaction,
        segundos: app_commands.Range[int, 0, 21600],
    ):
        if not await check_channel_perms(interaction):
            return

        await interaction.channel.edit(slowmode_delay=segundos)
        self.db.set_channel_config(interaction.channel.id, interaction.guild_id, slowmode=segundos)

        if segundos == 0:
            msg = "✅ Modo lento **desactivado**."
        else:
            msg = f"✅ Modo lento configurado a **{segundos} segundo(s)**."

        await interaction.response.send_message(msg, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /channelsetup  (panel interactivo)
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="channelsetup",
        description="Panel interactivo para configurar multimedia-only y autorreacción",
    )
    async def channelsetup(self, interaction: discord.Interaction):
        if not await check_channel_perms(interaction):
            return

        cfg = self.db.get_channel_config(interaction.channel.id)
        embed = self._build_setup_embed(interaction.channel, cfg)
        view = ChannelSetupView(self, interaction.user.id, interaction.channel.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    def _build_setup_embed(self, channel, cfg: dict) -> discord.Embed:
        """Embed de estado de configuración del canal."""
        media_on = bool(cfg.get("media_only", 0))
        media_cfg = {}
        if cfg.get("media_config"):
            try:
                media_cfg = json.loads(cfg["media_config"])
            except json.JSONDecodeError:
                pass
        allowed = media_cfg.get("allowed_types", ["image", "video"])

        react_list = []
        if cfg.get("auto_react"):
            try:
                react_list = json.loads(cfg["auto_react"])
            except json.JSONDecodeError:
                pass

        embed = discord.Embed(
            title=f"📺 Configuración de Canal: #{channel.name}",
            color=discord.Color.teal(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="🖼️ Solo Multimedia",
            value=(
                f"**{'✅ Activado' if media_on else '❌ Desactivado'}**\n"
                f"Tipos: {', '.join(allowed) if media_on else '—'}"
            ),
            inline=False,
        )
        embed.add_field(
            name="😀 Autorreacción",
            value=(
                f"**{'✅ Activado' if react_list else '❌ Desactivado'}**\n"
                f"Emojis: {' '.join(react_list) if react_list else '—'}"
            ),
            inline=False,
        )
        embed.set_footer(text="Usa los botones para modificar")
        return embed

    # ─────────────────────────────────────────────────────────────────────────
    # Listener: on_message — multimedia-only y autorreacción
    # ─────────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        cfg = self.db.get_channel_config(message.channel.id)

        # ── Solo multimedia ───────────────────────────────────────────────
        if cfg.get("media_only", 0):
            media_cfg = {}
            if cfg.get("media_config"):
                try:
                    media_cfg = json.loads(cfg["media_config"])
                except json.JSONDecodeError:
                    pass
            allowed = media_cfg.get("allowed_types", ["image", "video"])

            has_valid = False
            for att in message.attachments:
                ct = (att.content_type or "").split("/")[0]
                if ct in allowed:
                    has_valid = True
                    break

            if not has_valid:
                # Si el usuario tiene permisos de moderación, no eliminar
                if not message.author.guild_permissions.manage_messages:
                    try:
                        await message.delete()
                        await message.channel.send(
                            f"⚠️ {message.author.mention}, este canal solo permite multimedia.",
                            delete_after=5,
                        )
                    except discord.Forbidden:
                        pass
                    return

        # ── Autorreacción ─────────────────────────────────────────────────
        if cfg.get("auto_react"):
            try:
                emojis = json.loads(cfg["auto_react"])
            except json.JSONDecodeError:
                emojis = []
            for emoji in emojis:
                try:
                    await message.add_reaction(emoji)
                except (discord.Forbidden, discord.HTTPException):
                    pass


# ── Views y Modals ────────────────────────────────────────────────────────────

class ClearAllConfirmView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=30)
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Solo quien ejecutó el comando puede confirmar.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirmar", emoji="✅", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        await interaction.response.edit_message(
            content="⏳ Clonando canal y eliminando el original...", embed=None, view=None,
        )
        try:
            new_channel = await channel.clone(reason=f"ClearAll por {interaction.user}")
            await new_channel.send(
                embed=discord.Embed(
                    title="🧹 Canal limpiado",
                    description=(
                        f"Todos los mensajes fueron eliminados por {interaction.user.mention}.\n\n"
                        "⚠️ **Nota de gestión:** Este canal fue recreado. "
                        "Las configuraciones de permisos se clonaron del original."
                    ),
                    color=discord.Color.orange(),
                    timestamp=datetime.now(timezone.utc),
                )
            )
            await channel.delete(reason=f"ClearAll por {interaction.user}")
        except discord.Forbidden:
            await interaction.followup.send("❌ No tengo permisos para clonar/eliminar el canal.", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancelar", emoji="❌", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="✅ Operación cancelada.", embed=None, view=None)
        self.stop()


class ChannelSetupView(discord.ui.View):
    """Panel interactivo para configurar multimedia-only y autorreacción."""

    def __init__(self, cog: Channels, author_id: int, channel_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.author_id = author_id
        self.channel_id = channel_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Solo quien abrió el panel puede usarlo.", ephemeral=True)
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction):
        cfg = self.cog.db.get_channel_config(self.channel_id)
        channel = interaction.guild.get_channel(self.channel_id)
        embed = self.cog._build_setup_embed(channel, cfg)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Solo Multimedia", emoji="🖼️", style=discord.ButtonStyle.primary, row=0)
    async def media_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = self.cog.db.get_channel_config(self.channel_id)
        current = bool(cfg.get("media_only", 0))
        self.cog.db.set_channel_config(
            self.channel_id, interaction.guild_id, media_only=int(not current),
        )
        if not current:
            # Activar con tipos por defecto
            if not cfg.get("media_config"):
                self.cog.db.set_channel_config(
                    self.channel_id, interaction.guild_id,
                    media_config=json.dumps({"allowed_types": ["image", "video"]}),
                )
        await self._refresh(interaction)

    @discord.ui.button(label="Tipos Multimedia", emoji="📋", style=discord.ButtonStyle.secondary, row=0)
    async def media_types_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(MediaTypesModal(self))

    @discord.ui.button(label="Autorreacción", emoji="😀", style=discord.ButtonStyle.primary, row=1)
    async def react_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AutoReactModal(self))

    @discord.ui.button(label="Quitar Reacciones", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def clear_react_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.cog.db.set_channel_config(
            self.channel_id, interaction.guild_id, auto_react=None,
        )
        await self._refresh(interaction)

    @discord.ui.button(label="Cerrar", emoji="❌", style=discord.ButtonStyle.danger, row=2)
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="✅ Panel cerrado.", embed=None, view=None)
        self.stop()


class MediaTypesModal(discord.ui.Modal, title="Tipos de multimedia permitidos"):
    types_input = discord.ui.TextInput(
        label="Tipos (separados por coma)",
        placeholder="image, video, audio",
        default="image, video",
        max_length=100,
    )

    def __init__(self, parent: ChannelSetupView):
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction):
        types = [t.strip().lower() for t in self.types_input.value.split(",") if t.strip()]
        valid = {"image", "video", "audio", "application"}
        types = [t for t in types if t in valid]
        if not types:
            return await interaction.response.send_message(
                "❌ Tipos válidos: `image`, `video`, `audio`, `application`", ephemeral=True
            )
        self.parent.cog.db.set_channel_config(
            self.parent.channel_id, interaction.guild_id,
            media_config=json.dumps({"allowed_types": types}),
        )
        await self.parent._refresh(interaction)


class AutoReactModal(discord.ui.Modal, title="Configurar autorreacción"):
    emojis_input = discord.ui.TextInput(
        label="Emojis (separados por espacio)",
        placeholder="👍 ❤️ 🔥",
        max_length=200,
    )

    def __init__(self, parent: ChannelSetupView):
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction):
        emojis = self.emojis_input.value.strip().split()
        if not emojis:
            return await interaction.response.send_message("❌ Ingresa al menos un emoji.", ephemeral=True)
        self.parent.cog.db.set_channel_config(
            self.parent.channel_id, interaction.guild_id,
            auto_react=json.dumps(emojis),
        )
        await self.parent._refresh(interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(Channels(bot))
