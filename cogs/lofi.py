import logging
import asyncio

import discord
from discord.ext import commands, tasks
from discord import app_commands

logger = logging.getLogger(__name__)

# URL Directa de alta disponibilidad de un Stream de Radio Lofi 24/7
# Se usa un stream de Icecast/Shoutcast directo en lugar de YouTube para evitar desconexiones constantes.
LOFI_STREAM_URL = "http://lofi.stream.laut.fm/lofi"

class LofiRadio(commands.Cog):
    """Módulo de Radio Lofi 24/7 de alta calidad"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db # type: ignore
        self.lofi_manager.start()

    def cog_unload(self):
        self.lofi_manager.cancel()
        for vc in self.bot.voice_clients:
            if hasattr(vc, "is_playing") and vc.is_playing():
                # We can't use async in sync cog_unload easily without loop tasks,
                # but discord.py automatically disconnects on shutdown anyway.
                pass

    @tasks.loop(seconds=60)
    async def lofi_manager(self):
        await self.bot.wait_until_ready()
        
        for guild in self.bot.guilds:
            cfg = self.db.get_lofi_config(guild.id)
            if not cfg.get("enabled"):
                # Si estaba conectado y lo desactivaron, desconectar
                vc = guild.voice_client
                if vc:
                    await vc.disconnect(force=True)
                continue
            
            channel_id = cfg.get("channel_id")
            if not channel_id:
                continue
                
            channel = guild.get_channel(channel_id)
            if not channel or not isinstance(channel, discord.VoiceChannel):
                continue
                
            vc = guild.voice_client
            
            if not vc or not vc.is_connected():
                try:
                    vc = await channel.connect(reconnect=True)
                except Exception as e:
                    logger.warning(f"No se pudo conectar al canal Lofi en {guild.name}: {e}")
                    continue
                    
            if vc.channel.id != channel_id:
                try:
                    await vc.move_to(channel)
                except Exception:
                    continue

            if not vc.is_playing():
                try:
                    # FFmpeg options optimizations for streaming
                    ffmpeg_options = {
                        'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                        'options': '-vn'
                    }
                    audio_source = discord.FFmpegPCMAudio(LOFI_STREAM_URL, **ffmpeg_options)
                    
                    # Apply volume if needed (FFmpegPCMAudio default doesn't support volume directly, 
                    # PCMVolumeTransformer is needed, but we keep it simple at 100% unless modified).
                    vol = cfg.get("volume", 100) / 100.0
                    if vol != 1.0:
                        audio_source = discord.PCMVolumeTransformer(audio_source, volume=vol)
                        
                    vc.play(audio_source)
                    
                    # Intentar editar el estado del canal de voz (disponible en nuevas versiones de Discord API)
                    try:
                        await channel.edit(status="🎶 Lofi Radio 24/7 | Chill & Relax")
                    except discord.Forbidden:
                        pass
                except Exception as e:
                    logger.error(f"Error reproduciendo Lofi en {guild.name}: {e}")


    @app_commands.command(name="lofi", description="Configura la radio Lofi 24/7 en un canal de voz")
    @app_commands.describe(
        canal="Canal de voz donde vivirá el bot",
        estado="Encender (True) o Apagar (False) la radio",
        volumen="Volumen de 1 a 100 (por defecto 100)"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_lofi(self, interaction: discord.Interaction, canal: discord.VoiceChannel, estado: bool, volumen: app_commands.Range[int, 1, 100] = 100):
        self.db.set_lofi_config(
            interaction.guild_id,
            channel_id=canal.id,
            volume=volumen,
            enabled=1 if estado else 0
        )
        
        if estado:
            await interaction.response.send_message(f"📻 **Lofi Radio** activada en {canal.mention}. El bot se conectará en breve (hasta 60s).", ephemeral=True)
        else:
            await interaction.response.send_message("📻 **Lofi Radio** desactivada.", ephemeral=True)
            if interaction.guild.voice_client:
                await interaction.guild.voice_client.disconnect()


async def setup(bot: commands.Bot):
    await bot.add_cog(LofiRadio(bot))
