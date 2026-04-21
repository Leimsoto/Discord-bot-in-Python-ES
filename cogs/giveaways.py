import logging
import json
import asyncio
import random
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands, tasks
from discord import app_commands

logger = logging.getLogger(__name__)

class GiveawayJoinView(discord.ui.View):
    def __init__(self, cog, message_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.message_id = message_id

    @discord.ui.button(label="0", emoji="🎉", style=discord.ButtonStyle.primary, custom_id="gw_join")
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        gw = self.cog.db.get_giveaway(self.message_id)
        if not gw or gw["ended"]:
            return await interaction.response.send_message("❌ Este sorteo ya ha finalizado.", ephemeral=True)
            
        parts = json.loads(gw["participants"])
        if interaction.user.id in parts:
            parts.remove(interaction.user.id)
            msg = "Has abandonado el sorteo."
        else:
            # Check roles
            req_roles = json.loads(gw["req_roles"]) if gw["req_roles"] else []
            deny_roles = json.loads(gw["deny_roles"]) if gw["deny_roles"] else []
            
            user_roles = [r.id for r in interaction.user.roles]
            
            # Si hay roles requeridos, DEBE tener AL MENOS uno de ellos (o todos? Normalmente es al menos uno)
            if req_roles and not any(r in user_roles for r in req_roles):
                req_mentions = " o ".join([f"<@&{r}>" for r in req_roles])
                return await interaction.response.send_message(f"❌ Necesitas tener al menos uno de estos roles: {req_mentions}", ephemeral=True)
                
            # Si hay roles denegados, NO DEBE tener NINGUNO de ellos
            if deny_roles and any(r in user_roles for r in deny_roles):
                return await interaction.response.send_message("❌ Tienes un rol que no tiene permitido participar en este sorteo.", ephemeral=True)
                
            parts.append(interaction.user.id)
            msg = "🎉 ¡Te has unido al sorteo!"

        self.cog.db.update_giveaway(self.message_id, participants=json.dumps(parts))
        
        button.label = str(len(parts))
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(msg, ephemeral=True)


class Giveaways(commands.Cog):
    """Módulo de Sorteos Avanzados"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db # type: ignore
        self.giveaway_checker.start()

    def cog_unload(self):
        self.giveaway_checker.cancel()

    @tasks.loop(seconds=30)
    async def giveaway_checker(self):
        await self.bot.wait_until_ready()
        active = self.db.get_active_giveaways()
        now = int(datetime.now(timezone.utc).timestamp())
        
        for gw in active:
            if now >= gw["end_time"]:
                await self.end_giveaway(gw)

    async def end_giveaway(self, gw: dict):
        self.db.update_giveaway(gw["message_id"], ended=1)
        guild = self.bot.get_guild(gw["guild_id"])
        if not guild: return
        channel = guild.get_channel(gw["channel_id"])
        if not channel: return
        
        try:
            msg = await channel.fetch_message(gw["message_id"])
            parts = json.loads(gw["participants"])
            winners_count = gw["winners_count"]
            
            if not parts:
                await channel.send(f"Tristemente nadie participó en el sorteo de **{gw['prize']}**. 😢")
                embed = msg.embeds[0]
                embed.color = discord.Color.dark_grey()
                embed.set_footer(text="Sorteo Finalizado - Sin participantes")
                view = GiveawayJoinView(self, gw["message_id"])
                view.children[0].disabled = True
                await msg.edit(embed=embed, view=view)
                return
                
            winners_ids = random.sample(parts, min(len(parts), winners_count))
            winners_mentions = ", ".join(f"<@{w}>" for w in winners_ids)
            
            await channel.send(f"🎉 ¡Felicidades {winners_mentions}! Has ganado **{gw['prize']}**.")
            
            embed = msg.embeds[0]
            embed.color = discord.Color.dark_grey()
            embed.set_footer(text=f"Finalizado | Ganadores: {len(winners_ids)}")
            embed.description += f"\n\n🏆 **Ganadores:** {winners_mentions}"
            
            view = GiveawayJoinView(self, gw["message_id"])
            view.children[0].disabled = True
            await msg.edit(embed=embed, view=view)
            
        except Exception as e:
            logger.error(f"Error terminando sorteo: {e}")

    @app_commands.command(name="giveaway", description="Crea un sorteo interactivo")
    @app_commands.describe(
        premio="Qué se va a sortear", 
        duracion_horas="Duración en horas", 
        ganadores="Cantidad de ganadores",
        rol_requerido="Rol necesario para participar (Opcional)",
        rol_denegado="Rol que NO puede participar (Opcional)",
        imagen_url="URL de imagen para el sorteo (Opcional)"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def create_giveaway(
        self, 
        interaction: discord.Interaction, 
        premio: str, 
        duracion_horas: float,
        ganadores: int = 1,
        rol_requerido: discord.Role = None,
        rol_denegado: discord.Role = None,
        imagen_url: str = None
    ):
        end_time_dt = datetime.now(timezone.utc) + timedelta(hours=duracion_horas)
        end_ts = int(end_time_dt.timestamp())
        
        req_roles = [rol_requerido.id] if rol_requerido else []
        deny_roles = [rol_denegado.id] if rol_denegado else []
        
        embed = discord.Embed(
            title=f"🎁 Sorteo: {premio}",
            description=f"¡Pulsa el botón 🎉 para participar!\n"
                        f"Ganadores: **{ganadores}**\n"
                        f"Finaliza: <t:{end_ts}:R> (<t:{end_ts}:f>)",
            color=discord.Color.purple()
        )
        if rol_requerido:
            embed.add_field(name="Requisitos", value=f"Debes tener el rol {rol_requerido.mention}", inline=False)
        if rol_denegado:
            embed.add_field(name="Denegados", value=f"NO debes tener el rol {rol_denegado.mention}", inline=False)
            
        if imagen_url and imagen_url.startswith("http"):
            embed.set_image(url=imagen_url)
            
        await interaction.response.send_message("Sorteo creado.", ephemeral=True)
        msg = await interaction.channel.send(embed=embed)
        
        self.db.create_giveaway(
            interaction.guild_id, interaction.channel.id, msg.id, 
            premio, end_ts, ganadores, 
            json.dumps(req_roles), json.dumps(deny_roles)
        )
        
        view = GiveawayJoinView(self, msg.id)
        await msg.edit(view=view)

async def setup(bot: commands.Bot):
    await bot.add_cog(Giveaways(bot))
