import os
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional, List, Dict

import discord
from discord.ext import commands
from discord import app_commands

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

logger = logging.getLogger(__name__)

class IAConfigView(discord.ui.View):
    def __init__(self, cog, guild_id: int):
        super().__init__(timeout=120)
        self.cog = cog
        self.guild_id = guild_id
        
        cfg = self.cog.db.get_ai_config(self.guild_id)
        current_model = cfg.get("ai_model", "gemini-2.5-flash")
        
        # Botones UI
        self.model_btn = discord.ui.Button(
            label=f"Modelo: {current_model}", 
            style=discord.ButtonStyle.primary,
            emoji="🤖"
        )
        self.model_btn.callback = self.toggle_model
        self.add_item(self.model_btn)

    async def toggle_model(self, interaction: discord.Interaction):
        cfg = self.cog.db.get_ai_config(self.guild_id)
        current = cfg.get("ai_model", "gemini-2.5-flash")
        new_model = "gemini-2.5-pro" if "flash" in current.lower() else "gemini-2.5-flash"
        self.cog.db.set_ai_config(self.guild_id, ai_model=new_model)
        self.model_btn.label = f"Modelo: {new_model}"
        embed = self.cog._build_ia_embed(interaction.guild, self.cog.db.get_ai_config(self.guild_id))
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Canal Chat", style=discord.ButtonStyle.secondary, emoji="💬")
    async def chat_channel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Menciona el nuevo canal de chat de IA (ej: #chat-ai):", ephemeral=True)
        try:
            msg = await self.cog.bot.wait_for(
                "message",
                check=lambda m: m.author == interaction.user and m.channel == interaction.channel,
                timeout=60.0
            )
            if msg.channel_mentions:
                self.cog.db.set_ai_config(self.guild_id, ai_channel_id=msg.channel_mentions[0].id)
                await msg.delete()
                embed = self.cog._build_ia_embed(interaction.guild, self.cog.db.get_ai_config(self.guild_id))
                await interaction.edit_original_response(embed=embed, view=self)
        except asyncio.TimeoutError:
            pass

    @discord.ui.button(label="Rol Ping AI", style=discord.ButtonStyle.secondary, emoji="👥")
    async def role_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Menciona el rol permitido para usar @Bot (ej: @Premium):", ephemeral=True)
        try:
            msg = await self.cog.bot.wait_for(
                "message",
                check=lambda m: m.author == interaction.user and m.channel == interaction.channel,
                timeout=60.0
            )
            if msg.role_mentions:
                self.cog.db.set_ai_config(self.guild_id, ai_role_id=msg.role_mentions[0].id)
                await msg.delete()
                embed = self.cog._build_ia_embed(interaction.guild, self.cog.db.get_ai_config(self.guild_id))
                await interaction.edit_original_response(embed=embed, view=self)
        except asyncio.TimeoutError:
            pass


class IA(commands.Cog):
    """Módulo de Inteligencia Artificial (Google Gemini)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db # type: ignore
        self.server_contexts = {} # guild_id -> str
        self.chat_histories = {} # guild_id_user_id -> List[Content]

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key or not genai:
            logger.warning("No se encontró GEMINI_API_KEY o google-genai no está instalado.")
            self.client = None
        else:
            self.client = genai.Client(api_key=api_key)

    def _build_ia_embed(self, guild: discord.Guild, cfg: dict) -> discord.Embed:
        embed = discord.Embed(
            title="🧠 Configuración de Inteligencia Artificial",
            description="Controla cómo interactúa el bot con Gemini API.",
            color=discord.Color.purple()
        )
        
        ch_id = cfg.get("ai_channel_id")
        ch_str = f"<#{ch_id}>" if ch_id else "❌ No configurado"
        embed.add_field(name="💬 Canal Chat Único", value=ch_str, inline=True)
        
        r_id = cfg.get("ai_role_id")
        r_str = f"<@&{r_id}>" if r_id else "❌ No configurado"
        embed.add_field(name="👥 Rol Ping Permitido", value=r_str, inline=True)
        
        embed.add_field(name="🤖 Modelo Activo", value=f"`{cfg.get('ai_model', 'gemini-2.5-flash')}`", inline=True)
        return embed

    @app_commands.command(name="iaconfig", description="Configura el módulo de Inteligencia Artificial")
    @app_commands.checks.has_permissions(administrator=True)
    async def iaconfig(self, interaction: discord.Interaction):
        if not self.client:
            return await interaction.response.send_message("❌ La API de Gemini no está configurada (.env).", ephemeral=True)
            
        cfg = self.db.get_ai_config(interaction.guild_id)
        embed = self._build_ia_embed(interaction.guild, cfg)
        view = IAConfigView(self, interaction.guild_id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="iasync", description="Sincroniza y comprime la información del servidor para darle contexto a la IA")
    @app_commands.checks.has_permissions(administrator=True)
    async def iasync(self, interaction: discord.Interaction):
        if not self.client:
            return await interaction.response.send_message("❌ La API de Gemini no está configurada.", ephemeral=True)
            
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        
        context_parts = [f"Información del servidor: {guild.name}"]
        context_parts.append(f"Descripción: {guild.description or 'Ninguna'}")
        context_parts.append(f"Cantidad de miembros: {guild.member_count}")
        
        # Recopilar canales
        context_parts.append("\n--- Canales ---")
        for channel in guild.text_channels[:20]: # Límite para no exceder si hay muchísimos
            topic = f" - Tema: {channel.topic}" if channel.topic else ""
            context_parts.append(f"#{channel.name}{topic}")
            
        # Recopilar roles
        context_parts.append("\n--- Roles Principales ---")
        for role in reversed(guild.roles[1:15]): # Ignorar @everyone y limite a los top roles
            context_parts.append(f"- {role.name}")

        self.server_contexts[guild.id] = "\n".join(context_parts)
        await interaction.followup.send("✅ Contexto del servidor sincronizado y cargado en memoria para la IA.")

    def _get_system_prompt(self, guild_id: int) -> str:
        base_prompt = os.getenv("AI_SYSTEM_PROMPT", "Eres un asistente amigable y útil llamado TortuguBot. Si te hacen preguntas de actualidad, usa Google Search para responder.")
        srv_ctx = self.server_contexts.get(guild_id, "")
        if srv_ctx:
            base_prompt += f"\n\nContexto actual del servidor donde te encuentras:\n{srv_ctx}"
        return base_prompt

    async def _send_via_webhook(self, channel: discord.TextChannel, user: discord.User, text: str):
        webhooks = await channel.webhooks()
        webhook = discord.utils.get(webhooks, name="TortuguBot_IA")
        if not webhook:
            webhook = await channel.create_webhook(name="TortuguBot_IA")
        
        # Segmentar texto si supera 2000 chars
        chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
        for chunk in chunks:
            await webhook.send(
                content=chunk,
                username="TortuguBot IA",
                avatar_url=self.bot.user.display_avatar.url
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not self.client or not message.guild:
            return

        cfg = self.db.get_ai_config(message.guild.id)
        ai_channel_id = cfg.get("ai_channel_id")
        ai_role_id = cfg.get("ai_role_id")
        
        is_ai_channel = (message.channel.id == ai_channel_id)
        has_role = any(r.id == ai_role_id for r in message.author.roles) if ai_role_id else False
        is_pinging_bot = self.bot.user in message.mentions

        # 1. Chat Único
        # 2. AIPing (@bot)
        
        if is_ai_channel or (is_pinging_bot and has_role):
            ctx_id = f"{message.guild.id}_{message.author.id}"
            
            # Inicializar historial
            if ctx_id not in self.chat_histories:
                self.chat_histories[ctx_id] = []
            
            # Limpiar contenido del mensaje si es ping
            user_text = message.content.replace(f"<@{self.bot.user.id}>", "").strip()
            
            # Si hay un mensaje referenciado, añadirlo al contexto
            if message.reference and message.reference.message_id:
                try:
                    ref_msg = await message.channel.fetch_message(message.reference.message_id)
                    user_text = f"[En respuesta a {ref_msg.author.name}: '{ref_msg.content}']\n" + user_text
                except:
                    pass

            # Añadir mensaje al historial de Gemini
            self.chat_histories[ctx_id].append(
                types.Content(role="user", parts=[types.Part.from_text(text=user_text)])
            )

            # Mantener máximo 20 interacciones de memoria
            if len(self.chat_histories[ctx_id]) > 20:
                self.chat_histories[ctx_id] = self.chat_histories[ctx_id][-20:]

            try:
                async with message.channel.typing():
                    # Configurar modelo y Google Search nativo
                    model = cfg.get("ai_model", "gemini-2.5-flash")
                    sys_prompt = self._get_system_prompt(message.guild.id)
                    
                    config = types.GenerateContentConfig(
                        system_instruction=sys_prompt,
                        temperature=0.7,
                        tools=[{"google_search": {}}], # Habilitar Google Search nativo
                    )
                    
                    response = self.client.models.generate_content(
                        model=model,
                        contents=self.chat_histories[ctx_id],
                        config=config
                    )
                    
                    reply_text = response.text or "No pude generar una respuesta."
                    
                    # Añadir respuesta de la IA al historial
                    self.chat_histories[ctx_id].append(
                        types.Content(role="model", parts=[types.Part.from_text(text=reply_text)])
                    )
                    
                    # Enviar respuesta
                    if is_ai_channel and isinstance(message.channel, discord.TextChannel):
                        await self._send_via_webhook(message.channel, message.author, reply_text)
                    else:
                        # Si es un ping en otro canal, responder con reply
                        if len(reply_text) > 2000:
                            reply_text = reply_text[:1996] + "..."
                        await message.reply(reply_text)

            except Exception as e:
                logger.error(f"Error en IA: {e}", exc_info=True)
                await message.channel.send("❌ Error al contactar con la API de IA.")

async def setup(bot: commands.Bot):
    await bot.add_cog(IA(bot))
