import logging
import asyncio
import requests
import urllib.parse

import discord
from discord.ext import commands, tasks
from discord import app_commands

logger = logging.getLogger(__name__)

LOFI_STREAM_URL = "http://lofi.stream.laut.fm/lofi"
RADIO_API_URL = "http://de1.api.radio-browser.info/json/stations/search"

class LofiRadio(commands.Cog):
    """Módulo de Radio Global y Lofi 24/7"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db # type: ignore
        self.lofi_manager.start()

    def cog_unload(self):
        self.lofi_manager.cancel()
    @tasks.loop(seconds=60)
    async def lofi_manager(self):
        for guild in self.bot.guilds:
            cfg = self.db.get_lofi_config(guild.id)
            if not cfg.get("enabled"):
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
                except Exception:
                    logger.exception(f"No se pudo conectar al canal de radio en {guild.name}")
                    continue
                    
            if vc.channel.id != channel_id:
                try:
                    await vc.move_to(channel)
                except Exception:
                    continue

            if not vc.is_playing():
                self.start_playing(vc, channel, cfg)

    @lofi_manager.before_loop
    async def before_lofi_manager(self):
        await self.bot.wait_until_ready()

    def start_playing(self, vc, channel, cfg):
        # Allow custom stations from DB in future, for now fallback to default lofi
        stream_url = cfg.get("stream_url", LOFI_STREAM_URL)
        station_name = cfg.get("station_name", "Lofi Radio 24/7")

        try:
            ffmpeg_options = {
                'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                'options': '-vn'
            }
            audio_source = discord.FFmpegPCMAudio(stream_url, **ffmpeg_options)
            
            vol = cfg.get("volume", 100) / 100.0
            if vol != 1.0:
                audio_source = discord.PCMVolumeTransformer(audio_source, volume=vol)
                
            # Callback para reiniciar inmediatamente si corta
            def after_playback(error):
                if error:
                    logger.error(f"Error en reproducción de radio: {error}")
                if cfg.get("enabled"):
                    # Scheduling next play safely in the event loop
                    fut = asyncio.run_coroutine_threadsafe(self.reconnect_stream(vc, channel, cfg), self.bot.loop)
                    try:
                        fut.result(timeout=5)
                    except Exception:
                        pass
                        
            vc.play(audio_source, after=after_playback)
            
            # Cambiar estado
            fut = asyncio.run_coroutine_threadsafe(channel.edit(status=f"🎶 {station_name} | Chill & Relax"), self.bot.loop)
            try:
                fut.result(timeout=5)
            except Exception:
                pass
        except Exception:
            logger.exception(f"Error reproduciendo radio en el canal {channel.id}")

    async def reconnect_stream(self, vc, channel, cfg):
        await asyncio.sleep(2) # Breve pausa para evitar loop infinito rápido
        if vc and vc.is_connected() and not vc.is_playing() and cfg.get("enabled"):
            self.start_playing(vc, channel, cfg)

    radio_group = app_commands.Group(name="radio", description="Configuración de Radio y Lofi 24/7")

    @radio_group.command(name="setup", description="Configura una estación de radio 24/7 en un canal de voz")
    @app_commands.describe(
        canal="Canal de voz donde vivirá el bot",
        estado="Encender (True) o Apagar (False) la radio",
        volumen="Volumen de 1 a 100 (por defecto 100)"
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_radio(self, interaction: discord.Interaction, canal: discord.VoiceChannel, estado: bool, volumen: app_commands.Range[int, 1, 100] = 100):
        self.db.set_lofi_config(
            interaction.guild_id,
            channel_id=canal.id,
            volume=volumen,
            enabled=1 if estado else 0
        )
        
        if estado:
            await interaction.response.send_message(f"📻 **Radio** activada en {canal.mention}. El bot se conectará en breve.", ephemeral=True)
            # Despertar al bot rápido
            vc = interaction.guild.voice_client
            if vc and vc.channel.id != canal.id:
                await vc.move_to(canal)
        else:
            await interaction.response.send_message("📻 **Radio** desactivada.", ephemeral=True)
            if interaction.guild.voice_client:
                await interaction.guild.voice_client.disconnect()

    @radio_group.command(name="status", description="Consulta el estado y configuración actual de la radio")
    async def radio_status(self, interaction: discord.Interaction):
        cfg = self.db.get_lofi_config(interaction.guild_id)
        if not cfg.get("enabled"):
            return await interaction.response.send_message("❌ La radio está desactivada en este servidor.", ephemeral=True)
            
        canal = interaction.guild.get_channel(cfg["channel_id"])
        vc = interaction.guild.voice_client
        
        status_text = "🟢 Reproduciendo" if (vc and vc.is_playing()) else "🔴 Detenido / Conectando..."
        station = cfg.get("station_name", "Lofi Radio 24/7")
        
        embed = discord.Embed(title="📻 Estado de la Radio", color=discord.Color.blue())
        embed.add_field(name="Estación", value=f"**{station}**", inline=False)
        embed.add_field(name="Canal", value=canal.mention if canal else "No encontrado", inline=True)
        embed.add_field(name="Volumen", value=f"{cfg.get('volume', 100)}%", inline=True)
        embed.add_field(name="Estado", value=status_text, inline=True)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @radio_group.command(name="buscar", description="Busca estaciones de radio globales para reproducir")
    @app_commands.describe(nombre="Nombre, género o país a buscar")
    @app_commands.checks.has_permissions(administrator=True)
    async def radio_search(self, interaction: discord.Interaction, nombre: str):
        await interaction.response.defer(ephemeral=True)
        
        # Consultar la API global de radios
        try:
            params = {
                "name": nombre,
                "limit": 10,
                "hidebroken": "true",
                "order": "clickcount",
                "reverse": "true"
            }
            resp = requests.get(RADIO_API_URL, params=params, timeout=5)
            data = resp.json()
            
            if not data:
                return await interaction.followup.send("❌ No se encontraron estaciones con ese nombre.")
                
            options = []
            for idx, station in enumerate(data[:10]):
                name = station.get("name", "Desconocida")[:90]
                options.append(discord.SelectOption(
                    label=name,
                    description=f"{station.get('country', '')} - {station.get('tags', '')[:40]}",
                    value=f"{idx}"
                ))
                
            class RadioSelect(discord.ui.Select):
                def __init__(self, stations, db, bot, cog):
                    self.stations = stations
                    self.db = db
                    self.bot = bot
                    self.cog = cog
                    super().__init__(placeholder="Selecciona una emisora para reproducirla...", min_values=1, max_values=1, options=options)
                    
                async def callback(self, inter: discord.Interaction):
                    idx = int(self.values[0])
                    station = self.stations[idx]
                    url = station.get("url_resolved")
                    name = station.get("name", "Desconocida")
                    
                    # Asegurar columnas y actualizar configuración
                    try:
                        self.db._execute("ALTER TABLE lofi_config ADD COLUMN stream_url TEXT", ())
                        self.db._execute("ALTER TABLE lofi_config ADD COLUMN station_name TEXT", ())
                    except Exception:
                        pass
                        
                    self.db._upsert_config(
                        "lofi_config", inter.guild_id, 
                        stream_url=url, station_name=name, enabled=1
                    )

                    # Aplicar la nueva emisora inmediatamente
                    cfg = self.db.get_lofi_config(inter.guild_id)
                    channel = inter.guild.get_channel(cfg.get("channel_id"))
                    vc = inter.guild.voice_client

                    if not channel:
                        await inter.response.edit_message(content="❌ Canal configurado no encontrado.", view=None)
                        return

                    try:
                        if not vc or not vc.is_connected():
                            vc = await channel.connect(reconnect=True)
                        else:
                            # Si está reproduciendo, pararla para evitar estados inconsistentes
                            if vc.is_playing():
                                vc.stop()
                            # mover si está en otro canal
                            if vc.channel.id != channel.id:
                                try:
                                    await vc.move_to(channel)
                                except Exception:
                                    pass

                        # pequeña pausa para que FFmpeg libere recursos
                        await asyncio.sleep(0.3)

                        # Iniciar reproducción con la nueva configuración
                        self.cog.start_playing(vc, channel, cfg)

                    except Exception as e:
                        logger.exception(f"Error aplicando emisora en {inter.guild.name}: {e}")
                        await inter.response.edit_message(content=f"❌ Error al aplicar la emisora: {e}", view=None)
                        return

                    await inter.response.edit_message(content=f"📻 **Radio cambiada a:** {name}\nSe aplicará en unos segundos.", view=None)

            view = discord.ui.View()
            view.add_item(RadioSelect(data, self.db, self.bot, self))
            
            await interaction.followup.send("Selecciona la estación que deseas sintonizar:", view=view)
            
        except Exception as e:
            logger.error(f"Error en radio_search: {e}")
            await interaction.followup.send(f"❌ Error consultando la API de radio: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(LofiRadio(bot))
