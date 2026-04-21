import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands
from discord import app_commands

logger = logging.getLogger(__name__)

class SuggestionPublicView(discord.ui.View):
    def __init__(self, cog, suggestion_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.suggestion_id = suggestion_id

    @discord.ui.button(label="A favor", emoji="👍", style=discord.ButtonStyle.success, custom_id="sugg_upvote")
    async def upvote_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # En una app real se trackearía si el usuario ya votó, por simplicidad solo sumaremos a la BD
        row = self.cog.db.get_suggestion(self.suggestion_id)
        if not row: return await interaction.response.send_message("Sugerencia no encontrada.", ephemeral=True)
        new_up = row["upvotes"] + 1
        self.cog.db.update_suggestion(self.suggestion_id, upvotes=new_up)
        
        embed = interaction.message.embeds[0]
        # Buscar campo de votos y actualizar
        for idx, field in enumerate(embed.fields):
            if "Votos" in field.name:
                embed.set_field_at(idx, name="Votos", value=f"A favor: **{new_up}**\nEn contra: **{row['downvotes']}**", inline=False)
                break
        await interaction.response.edit_message(embed=embed)

    @discord.ui.button(label="En contra", emoji="👎", style=discord.ButtonStyle.danger, custom_id="sugg_downvote")
    async def downvote_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = self.cog.db.get_suggestion(self.suggestion_id)
        if not row: return await interaction.response.send_message("Sugerencia no encontrada.", ephemeral=True)
        new_down = row["downvotes"] + 1
        self.cog.db.update_suggestion(self.suggestion_id, downvotes=new_down)
        
        embed = interaction.message.embeds[0]
        for idx, field in enumerate(embed.fields):
            if "Votos" in field.name:
                embed.set_field_at(idx, name="Votos", value=f"A favor: **{row['upvotes']}**\nEn contra: **{new_down}**", inline=False)
                break
        await interaction.response.edit_message(embed=embed)


class SuggestionReviewView(discord.ui.View):
    def __init__(self, cog, suggestion_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.suggestion_id = suggestion_id

    @discord.ui.button(label="Aprobar", style=discord.ButtonStyle.success, custom_id="sugg_approve")
    async def approve_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = self.cog.db.get_suggestion(self.suggestion_id)
        if not row: return await interaction.response.send_message("No encontrada.", ephemeral=True)
        
        cfg = self.cog.db.get_suggestions_config(interaction.guild_id)
        public_ch = interaction.guild.get_channel(cfg.get("public_channel_id", 0))
        if not public_ch:
            return await interaction.response.send_message("Canal público no configurado/encontrado.", ephemeral=True)
        
        user = interaction.guild.get_member(row["user_id"])
        username = user.display_name if user else f"Usuario {row['user_id']}"
        avatar_url = user.display_avatar.url if user else None

        embed = discord.Embed(
            title="Nueva sugerencia",
            description=row["content"],
            color=discord.Color.teal(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_author(name=f"Sugerencia de {username}", icon_url=avatar_url)
        embed.add_field(name="Votos", value="A favor: **0**\nEn contra: **0**", inline=False)
        embed.set_footer(text=f"Sugerencia para {interaction.guild.name}")
        
        view = SuggestionPublicView(self.cog, self.suggestion_id)
        msg = await public_ch.send(embed=embed, view=view)
        
        self.cog.db.update_suggestion(self.suggestion_id, status="ACCEPTED", message_id=msg.id)
        
        old_embed = interaction.message.embeds[0]
        old_embed.color = discord.Color.green()
        old_embed.title = "Sugerencia APROBADA"
        await interaction.response.edit_message(embed=old_embed, view=None)

    @discord.ui.button(label="Denegar", style=discord.ButtonStyle.danger, custom_id="sugg_deny")
    async def deny_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.cog.db.update_suggestion(self.suggestion_id, status="DENIED")
        old_embed = interaction.message.embeds[0]
        old_embed.color = discord.Color.red()
        old_embed.title = "Sugerencia DENEGADA"
        await interaction.response.edit_message(embed=old_embed, view=None)


class Suggestions(commands.Cog):
    """Módulo de Sugerencias con Review"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db # type: ignore

    setup_group = app_commands.Group(name="sugerencias", description="Gestión de sugerencias")

    @setup_group.command(name="setup", description="Configura los canales del sistema de sugerencias")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_suggestions(self, interaction: discord.Interaction, envio: discord.TextChannel, revision: discord.TextChannel, publico: discord.TextChannel):
        self.db.set_suggestions_config(
            interaction.guild_id,
            submit_channel_id=envio.id,
            review_channel_id=revision.id,
            public_channel_id=publico.id
        )
        await interaction.response.send_message(
            f"✅ **Sugerencias Configuradas**\n"
            f"📥 Envío: {envio.mention}\n"
            f"🛡️ Revisión (Staff): {revision.mention}\n"
            f"📢 Público: {publico.mention}",
            ephemeral=True
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        cfg = self.db.get_suggestions_config(message.guild.id)
        if not cfg or message.channel.id != cfg.get("submit_channel_id"):
            return

        # Es un mensaje en el canal de envío
        await message.delete()
        
        sugg_id = self.db.create_suggestion(message.guild.id, message.author.id, message.content)
        
        review_ch = message.guild.get_channel(cfg.get("review_channel_id"))
        if review_ch:
            embed = discord.Embed(
                title="Sugerencia PENDIENTE",
                description=message.content,
                color=discord.Color.yellow(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_author(name=f"Sugerencia de {message.author.display_name}", icon_url=message.author.display_avatar.url)
            view = SuggestionReviewView(self, sugg_id)
            await review_ch.send(embed=embed, view=view)

async def setup(bot: commands.Bot):
    await bot.add_cog(Suggestions(bot))
