import logging
import json
from datetime import datetime, timezone

import discord
from discord.ext import commands
from discord import app_commands

logger = logging.getLogger(__name__)

class Welcomes(commands.Cog):
    """Módulo de Bienvenidas, Rastreo de Invitaciones y Agradecimiento a Boosters"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db # type: ignore
        self.invites_cache = {} # guild_id -> {code: uses}
        self.bot.loop.create_task(self.update_all_invites())

    async def update_all_invites(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                invites = await guild.invites()
                self.invites_cache[guild.id] = {i.code: (i.uses, i.inviter) for i in invites}
            except discord.Forbidden:
                logger.warning(f"Sin permisos para leer invitaciones en {guild.name}")

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        if invite.guild:
            if invite.guild.id not in self.invites_cache:
                self.invites_cache[invite.guild.id] = {}
            self.invites_cache[invite.guild.id][invite.code] = (invite.uses, invite.inviter)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        if invite.guild and invite.guild.id in self.invites_cache:
            self.invites_cache[invite.guild.id].pop(invite.code, None)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        inviter = None
        
        # Invite Tracking Logic
        if guild.id in self.invites_cache:
            try:
                new_invites = await guild.invites()
                for i in new_invites:
                    old_uses = self.invites_cache[guild.id].get(i.code, (0, None))[0]
                    if i.uses and i.uses > old_uses:
                        inviter = i.inviter
                        break
                # Actualizar caché
                self.invites_cache[guild.id] = {i.code: (i.uses, i.inviter) for i in new_invites}
            except discord.Forbidden:
                pass

        cfg = self.db.get_welcome_config(guild.id)
        if not cfg.get("enabled") or not cfg.get("channel_id"):
            return

        channel = guild.get_channel(cfg["channel_id"])
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        embed_data = cfg.get("embed_data")
        if embed_data:
            try:
                data = json.loads(embed_data)
                # Formatear variables dinámicas
                title = data.get("title", "").replace("{user}", member.display_name).replace("{server}", guild.name)
                desc = data.get("description", "").replace("{user}", member.mention).replace("{server}", guild.name)
                if inviter:
                    desc += f"\n\n💌 Invitado por: {inviter.mention}"
                
                    embed = discord.Embed(
                        title=title or None,
                        description=desc or None,
                        color=discord.Color(data.get("color", 0x5865F2)),
                        timestamp=datetime.now(timezone.utc) if data.get("timestamp") else None
                    )
                if data.get("image_url"): embed.set_image(url=data.get("image_url"))
                if data.get("thumbnail_url"): embed.set_thumbnail(url=member.display_avatar.url)
                if data.get("footer_text"): embed.set_footer(text=data.get("footer_text"), icon_url=data.get("footer_icon"))
                
                await channel.send(content=member.mention, embed=embed)
            except Exception as e:
                logger.error(f"Error al enviar bienvenida: {e}")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # Detectar Server Boost (premium_since)
        if before.premium_since is None and after.premium_since is not None:
            cfg = self.db.get_boost_config(after.guild.id)
            if not cfg.get("enabled") or not cfg.get("channel_id"):
                return
            
            channel = after.guild.get_channel(cfg["channel_id"])
            if not channel or not isinstance(channel, discord.TextChannel):
                return
            
            embed_data = cfg.get("embed_data")
            gif_url = cfg.get("gif_url")
            
            if embed_data:
                try:
                    data = json.loads(embed_data)
                    title = data.get("title", "¡Nuevo Booster!").replace("{user}", after.display_name)
                    desc = data.get("description", "Gracias por mejorar nuestro servidor, te amamos.").replace("{user}", after.mention)
                    
                    embed = discord.Embed(
                        title=title,
                        description=desc,
                        color=discord.Color.purple()
                    )
                    if gif_url:
                        embed.set_image(url=gif_url)
                    
                    await channel.send(content=after.mention, embed=embed)
                except Exception as e:
                    logger.error(f"Error al enviar gracias por boost: {e}")

    # ── Comandos Setup ────────────────────────────────────────────────────────
    setup_group = app_commands.Group(name="configurar", description="Configuraciones de módulos especiales")

    @setup_group.command(name="bienvenidas", description="Configura el sistema de bienvenidas y tracker")
    @app_commands.describe(canal="Canal donde se enviarán", nombre_embed_guardado="Nombre del embed guardado en /embed list")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_welcome(self, interaction: discord.Interaction, canal: discord.TextChannel, nombre_embed_guardado: str):
        embed_row = self.db.get_saved_embed_by_name(interaction.guild_id, nombre_embed_guardado)
        if not embed_row:
            return await interaction.response.send_message(f"❌ No se encontró ningún embed guardado con el nombre `{nombre_embed_guardado}`. Usa `/embed create` primero.", ephemeral=True)
        
        self.db.set_welcome_config(
            interaction.guild_id,
            channel_id=canal.id,
            embed_data=embed_row["embed_data"],
            enabled=1
        )
        await interaction.response.send_message(f"✅ Bienvenidas activadas en {canal.mention} utilizando el diseño `{nombre_embed_guardado}`.\n*Consejo: Puedes usar `{{user}}` y `{{server}}` en tu embed.*", ephemeral=True)

    @setup_group.command(name="boosters", description="Configura el mensaje de agradecimiento a Boosters")
    @app_commands.describe(canal="Canal donde se enviarán", gif_url="URL directa de un GIF animado", nombre_embed_guardado="Nombre del embed guardado en /embed list")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_boosts(self, interaction: discord.Interaction, canal: discord.TextChannel, nombre_embed_guardado: str, gif_url: str):
        embed_row = self.db.get_saved_embed_by_name(interaction.guild_id, nombre_embed_guardado)
        if not embed_row:
            return await interaction.response.send_message(f"❌ No se encontró ningún embed guardado con el nombre `{nombre_embed_guardado}`.", ephemeral=True)
        
        if not gif_url.startswith("http"):
            return await interaction.response.send_message("❌ La URL del GIF debe comenzar con http/https.", ephemeral=True)
            
        self.db.set_boost_config(
            interaction.guild_id,
            channel_id=canal.id,
            embed_data=embed_row["embed_data"],
            gif_url=gif_url,
            enabled=1
        )
        await interaction.response.send_message(f"✅ Agradecimiento a boosters activado en {canal.mention} con el GIF proporcionado.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Welcomes(bot))
