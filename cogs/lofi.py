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
        # Locks or helpers could ir aquí si es necesario más adelante
        self._playback_wait = 1.0  # segundos a esperar tras parar una reproducción
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
            # Resolve stream_url if needed (may be a playlist); leave resolution to caller where possible
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
            
            # Actualizar presencia del bot para indicar la estación (no editar el canal)
            try:
                coro = self.bot.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.listening,
                        name=f"🎶 {station_name} | Chill & Relax",
                    )
                )
                fut = asyncio.run_coroutine_threadsafe(coro, self.bot.loop)
                fut.result(timeout=5)
            except Exception:
                # Fallar silenciosamente si no es posible cambiar la presencia
                pass
        except Exception:
            logger.exception(f"Error reproduciendo radio en el canal {channel.id}")

    def _resolve_stream_sync(self, url: str) -> str:
        """Resuelve playlists (.m3u/.pls) devolviendo la primera URL directa.

        Método síncrono pensado para ejecutarse en executor y no bloquear el loop.
        """
        try:
            r = requests.get(url, timeout=6)
            content_type = r.headers.get("content-type", "").lower()
            text = r.text
            # Heurística: si la URL termina en m3u/pls o el contenido parece una playlist
            if url.lower().endswith(('.m3u', '.m3u8', '.pls')) or text.strip().startswith('#') or '[playlist]' in text.lower() or 'audio/x-scpls' in content_type:
                lines = [l.strip() for l in text.splitlines() if l and not l.strip().startswith('#') and not l.strip().startswith('[')]
                if lines:
                    return lines[0]
        except Exception:
            pass
        return url

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
            try:
                # Ejecutar la petición blocking en un hilo para no bloquear el event loop
                resp = await asyncio.to_thread(requests.get, RADIO_API_URL, params=params, timeout=5)
                data = resp.json()
            except Exception as e:
                logger.error(f"Error consultando la API de radios: {e}")
                return await interaction.followup.send("❌ Error consultando la API de radios.")

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
                    # Acknowledge the component immediately to avoid 'Unknown interaction' when work takes >3s
                    try:
                        await inter.response.defer(ephemeral=True)
                    except Exception:
                        pass

                    idx = int(self.values[0])
                    station = self.stations[idx]
                    url = station.get("url_resolved")
                    name = station.get("name", "Desconocida")

                    # Resolve playlists off-loop to avoid blocking
                    try:
                        loop = asyncio.get_running_loop()
                        resolved = await loop.run_in_executor(None, self.cog._resolve_stream_sync, url)
                    except Exception:
                        resolved = url

                    # Asegurar columnas y actualizar configuración de forma segura
                    try:
                        # use DB helper to avoid ALTER repetidos
                        self.db.ensure_column("lofi_config", "stream_url", "TEXT")
                        self.db.ensure_column("lofi_config", "station_name", "TEXT")
                    except Exception:
                        pass

                    try:
                        self.db._upsert_config(
                            "lofi_config", inter.guild_id,
                            stream_url=resolved, station_name=name, enabled=1
                        )
                    except Exception:
                        logger.exception("Fallo guardando configuración de la emisora")

                    # Aplicar la nueva emisora inmediatamente
                    cfg = self.db.get_lofi_config(inter.guild_id)
                    channel = inter.guild.get_channel(cfg.get("channel_id"))
                    vc = inter.guild.voice_client

                    if not channel:
                        await inter.followup.send("❌ Canal configurado no encontrado.", ephemeral=True)
                        try:
                            await inter.message.edit(view=None)
                        except Exception:
                            pass
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
                        await asyncio.sleep(self.cog._playback_wait)

                        # Iniciar reproducción con la nueva configuración
                        self.cog.start_playing(vc, channel, cfg)

                    except Exception as e:
                        logger.exception(f"Error aplicando emisora en {inter.guild.name}: {e}")
                        await inter.followup.send(f"❌ Error al aplicar la emisora: {e}", ephemeral=True)
                        try:
                            await inter.message.edit(view=None)
                        except Exception:
                            pass
                        return

                    # Confirmación al usuario (ephemeral) y limpiar view
                    try:
                        await inter.followup.send(f"📻 **Radio cambiada a:** {name}\nSe aplicará en unos segundos.", ephemeral=True)
                        try:
                            await inter.message.edit(view=None)
                        except Exception:
                            pass
                    except Exception:
                        # Silenciar errores de interacción (posible caducidad)
                        pass

            view = discord.ui.View()
            view.add_item(RadioSelect(data, self.db, self.bot, self))
            
            await interaction.followup.send("Selecciona la estación que deseas sintonizar:", view=view)
            
        except Exception as e:
            logger.error(f"Error en radio_search: {e}")
            await interaction.followup.send(f"❌ Error consultando la API de radio: {e}")

    def _extract_ytdl_sync(self, url: str):
        """Extrae la URL directa de audio usando yt-dlp (sync, para ejecutarse en executor).

        Retorna (stream_url, title, error_msg). Si falta yt-dlp, error_msg contendrá instrucciones.
        """
        try:
            from yt_dlp import YoutubeDL
        except Exception:
            return None, None, "La dependencia 'yt-dlp' no está instalada. Instálala: pip install yt-dlp"

        ydl_opts = {"format": "bestaudio/best", "noplaylist": True, "quiet": True, "no_warnings": True}
        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if "entries" in info and info["entries"]:
                    info = info["entries"][0]
                stream_url = info.get("url")
                title = info.get("title") or info.get("webpage_url")
                return stream_url, title, None
        except Exception as exc:
            return None, None, str(exc)

    @radio_group.command(name="play", description="Reproduce audio desde URL (YouTube/Direct). Admin only. Solo si la radio está desactivada.")
    @app_commands.describe(url="Enlace de YouTube o URL directa de audio")
    @app_commands.checks.has_permissions(administrator=True)
    async def play_url(self, interaction: discord.Interaction, url: str):
        await interaction.response.defer(ephemeral=True)

        cfg = self.db.get_lofi_config(interaction.guild_id)
        if cfg.get("enabled"):
            return await interaction.followup.send("❌ La radio 24/7 está activa. Desactívala antes de usar reproducción manual.", ephemeral=True)

        # Determinar canal de voz: preferir el canal del autor si está en uno, sino el configurado
        channel = None
        if interaction.user and getattr(interaction.user, 'voice', None) and interaction.user.voice.channel:
            channel = interaction.user.voice.channel
        else:
            ch_id = cfg.get("channel_id")
            if ch_id:
                channel = interaction.guild.get_channel(ch_id)

        if not channel or not isinstance(channel, discord.VoiceChannel):
            return await interaction.followup.send("❌ No se encontró un canal de voz para reproducir. Conéctate a uno o configura uno con /radio setup.", ephemeral=True)

        vc = interaction.guild.voice_client
        try:
            if not vc or not vc.is_connected():
                vc = await channel.connect(reconnect=True)
            else:
                if vc.is_playing():
                    vc.stop()
                if vc.channel.id != channel.id:
                    try:
                        await vc.move_to(channel)
                    except Exception:
                        pass

            # Extraer URL con yt-dlp si es YouTube u otras plataformas
            loop = asyncio.get_running_loop()
            stream_url = url
            title = None

            try:
                # Intentar extracción (silenciosa si falla)
                res_url, res_title, err = await loop.run_in_executor(None, self._extract_ytdl_sync, url)
                if err:
                    # Si yt-dlp no está instalado, informamos al admin y usamos la URL directa
                    if res_url is None and 'yt-dlp' in (err or ''):
                        await interaction.followup.send(err + "\nSe usará la URL tal cual.", ephemeral=True)
                    else:
                        # otros errores, no bloquear
                        logger.warning(f"yt-dlp extraction warning: {err}")
                if res_url:
                    stream_url = res_url
                    title = res_title
            except Exception:
                pass

            # Reproducir
            ffmpeg_options = {
                'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                'options': '-vn'
            }
            audio_source = discord.FFmpegPCMAudio(stream_url, **ffmpeg_options)
            if vc.is_playing():
                vc.stop()
            vc.play(audio_source)

            display = title or url
            await interaction.followup.send(f"▶️ Reproduciendo: {display}", ephemeral=True)
        except Exception as e:
            logger.exception(f"Error en play_url: {e}")
            await interaction.followup.send(f"❌ Error al reproducir: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(LofiRadio(bot))
