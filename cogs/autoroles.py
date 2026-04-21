import logging
import json

import discord
from discord.ext import commands
from discord import app_commands

logger = logging.getLogger(__name__)

class AutoRoles(commands.Cog):
    """Módulo de Autoroles Inteligentes por Reacción"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db # type: ignore

    @app_commands.command(name="autorolereact", description="Configura un rol por reacción en un mensaje existente")
    @app_commands.describe(
        mensaje_id="ID del mensaje al que se reaccionará",
        emoji="Emoji a reaccionar (solo pon el emoji, ej: 👍 o un emoji personalizado)",
        rol="Rol a entregar"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def autorolereact_setup(self, interaction: discord.Interaction, mensaje_id: str, emoji: str, rol: discord.Role):
        try:
            msg_id = int(mensaje_id)
            msg = await interaction.channel.fetch_message(msg_id)
        except (ValueError, discord.NotFound):
            return await interaction.response.send_message("❌ Mensaje no encontrado en este canal. Asegúrate de poner el ID correcto.", ephemeral=True)
        
        if rol.is_bot_managed() or rol.is_premium_subscriber() or rol.is_integration() or rol.is_default():
            return await interaction.response.send_message("❌ No puedes usar roles manejados por bots, roles de booster, o everyone.", ephemeral=True)
            
        if rol.position >= interaction.guild.me.top_role.position:
            return await interaction.response.send_message("❌ El rol es superior al rol más alto del bot. Mueve el rol del bot hacia arriba en la configuración del servidor.", ephemeral=True)

        existing = self.db.get_autorole(msg_id)
        mapping = {}
        if existing:
            mapping = json.loads(existing["mapping_data"])
            
        # Parse custom emoji
        emoji_str = emoji
        if "<:" in emoji and ">" in emoji:
            emoji_str = emoji.split(":")[1] # fallback simple, but usually reaction raw name or id is better
            # En discord.py, RawReactionActionEvent.emoji.name para custom es el nombre
        
        mapping[emoji_str] = rol.id
        
        self.db.set_autorole(msg_id, interaction.guild_id, interaction.channel.id, json.dumps(mapping))
        
        try:
            await msg.add_reaction(emoji)
        except Exception as e:
            logger.warning(f"No se pudo añadir reacción inicial: {e}")
            
        await interaction.response.send_message(f"✅ AutoRol configurado. Al reaccionar con {emoji} en el mensaje, se dará el rol {rol.mention}.", ephemeral=True)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return
            
        row = self.db.get_autorole(payload.message_id)
        if not row: return
        
        mapping = json.loads(row["mapping_data"])
        emoji_key = str(payload.emoji)
        if payload.emoji.is_custom_emoji():
            # Si el emoji provisto por el usuario en el setup fue el formato <a:name:id>, str(payload.emoji) funciona.
            pass
            
        # Buscar compatibilidad simple
        matched_role_id = mapping.get(str(payload.emoji))
        if not matched_role_id:
            matched_role_id = mapping.get(payload.emoji.name)
            
        if matched_role_id:
            guild = self.bot.get_guild(payload.guild_id)
            if guild:
                role = guild.get_role(matched_role_id)
                member = guild.get_member(payload.user_id)
                if role and member:
                    try:
                        await member.add_roles(role, reason="AutoRole Reaction")
                    except discord.Forbidden:
                        pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return
            
        row = self.db.get_autorole(payload.message_id)
        if not row: return
        
        mapping = json.loads(row["mapping_data"])
        matched_role_id = mapping.get(str(payload.emoji))
        if not matched_role_id:
            matched_role_id = mapping.get(payload.emoji.name)
            
        if matched_role_id:
            guild = self.bot.get_guild(payload.guild_id)
            if guild:
                role = guild.get_role(matched_role_id)
                member = guild.get_member(payload.user_id)
                if role and member:
                    try:
                        await member.remove_roles(role, reason="AutoRole Reaction")
                    except discord.Forbidden:
                        pass

async def setup(bot: commands.Bot):
    await bot.add_cog(AutoRoles(bot))
