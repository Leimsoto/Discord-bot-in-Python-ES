import logging
import asyncio
import requests

import discord
from discord.ext import commands, tasks
from discord import app_commands

logger = logging.getLogger(__name__)

LOFI_STREAM_URL = "http://lofi.stream.laut.fm/lofi"
RADIO_API_URL = "http://de1.api.radio-browser.info/json/stations/search"

class Radio(commands.Cog):
    """Módulo de Radio Global 24/7 con búsqueda de emisoras."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db # type: ignore
        self._playback_wait = 1.0  # segundos a esperar tras parar una reproducción
        self._restart_in_progress: set[int] = set()
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self.radio_manager.start()

    def cog_unload(self):
        self.radio_manager.cancel()

    @tasks.loop(seconds=60)
    async def radio_manager(self):
        for guild in self.bot.guilds:
            await self._check_and_connect_guild(guild)

    @radio_manager.before_loop
    async def before_radio_manager(self):
        await self.bot.wait_until_ready()

    @staticmethod
    def _coerce_channel_id(value) -> int | None:
        if value in (None, "", 0, "0"):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_voice_channel(channel) -> bool:
        return isinstance(channel, (discord.VoiceChannel, discord.StageChannel))

    def _nearby_voice_channel(self, guild: discord.Guild, channel_id: int):
        candidates = [
            ch
            for ch in getattr(guild, "channels", [])
            if self._is_voice_channel(ch) and abs(int(ch.id) - int(channel_id)) <= 4096
        ]
        return candidates[0] if len(candidates) == 1 else None

    async def _resolve_voice_channel(self, guild: discord.Guild, channel_id: int):
        channel = guild.get_channel(channel_id)
        if self._is_voice_channel(channel):
            return channel

        try:
            fetched = await guild.fetch_channel(channel_id)
            if self._is_voice_channel(fetched):
                return fetched
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

        repaired = self._nearby_voice_channel(guild, channel_id)
        if repaired is not None:
            logger.warning(
                "[radio] %s: channel_id redondeado reparado: %s -> %s",
                guild.name,
                channel_id,
                repaired.id,
            )
            try:
                self.db.set_lofi_config(guild.id, channel_id=repaired.id)
            except Exception:
                logger.debug("[radio] No se pudo persistir channel_id reparado", exc_info=True)
            return repaired

        return None

    def _configured_radio_channel(self, guild: discord.Guild, cfg: dict):
        channel_id = self._coerce_channel_id(cfg.get("channel_id"))
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        return channel if self._is_voice_channel(channel) else None

    def _fallback_radio_channel(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return None

        vc = self._voice_client_for_guild(guild)
        if vc and vc.is_connected() and self._is_voice_channel(getattr(vc, "channel", None)):
            return vc.channel

        member_voice = getattr(getattr(interaction.user, "voice", None), "channel", None)
        if self._is_voice_channel(member_voice):
            return member_voice

        return None

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(int(guild_id))
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[int(guild_id)] = lock
        return lock

    def _voice_client_for_guild(self, guild: discord.Guild):
        """Devuelve el voice client real aunque ``guild.voice_client`` venga desfasado.

        discord.py puede tener el cliente en ``bot.voice_clients`` mientras
        ``guild.voice_client`` todavía es ``None`` durante reconnects/resumes.
        Si llamamos ``channel.connect`` en ese estado, la librería lanza
        ``ClientException: Already connected to a voice channel`` cada tick.
        """
        vc = getattr(guild, "voice_client", None)
        if vc is not None:
            return vc

        for candidate in getattr(self.bot, "voice_clients", []) or []:
            if getattr(getattr(candidate, "guild", None), "id", None) == guild.id:
                return candidate
        return None

    async def _ensure_voice_client(self, guild: discord.Guild, channel):
        """Reutiliza/mueve/conecta el voice client sin duplicar conexiones."""
        vc = self._voice_client_for_guild(guild)
        if vc and vc.is_connected():
            current = getattr(vc, "channel", None)
            if getattr(current, "id", None) != getattr(channel, "id", None):
                logger.info(f"[radio] Moviendo al canal {channel.name} en {guild.name}")
                await vc.move_to(channel)
            return vc

        if vc is not None:
            try:
                await vc.disconnect(force=True)
            except Exception:
                logger.debug("[radio] No se pudo limpiar voice client viejo en %s", guild.name, exc_info=True)

        try:
            logger.info(f"[radio] Conectando a {channel.name} en {guild.name}")
            return await channel.connect(reconnect=True)
        except discord.ClientException as exc:
            existing = self._voice_client_for_guild(guild)
            if existing and existing.is_connected():
                logger.warning(
                    "[radio] %s ya tenía voice client activo; se reutiliza para cortar el loop de conexión (%s)",
                    guild.name,
                    exc,
                )
                current = getattr(existing, "channel", None)
                if getattr(current, "id", None) != getattr(channel, "id", None):
                    await existing.move_to(channel)
                return existing
            raise

    def _resolve_interaction_radio_channel(
        self,
        interaction: discord.Interaction,
        cfg: dict,
        *,
        persist_fallback: bool = False,
    ):
        if interaction.guild is None:
            return None

        channel = self._configured_radio_channel(interaction.guild, cfg)
        if channel:
            return channel

        channel = self._fallback_radio_channel(interaction)
        if channel and persist_fallback:
            self.db.set_lofi_config(interaction.guild.id, channel_id=channel.id)
            cfg["channel_id"] = channel.id
            logger.info(
                "[radio] guild=%s canal de radio actualizado por fallback: %s",
                interaction.guild.id,
                channel.id,
            )
        return channel

    @staticmethod
    def _voice_status_text(station_name: str | None) -> str:
        clean_name = " ".join(str(station_name or "Lofi Radio 24/7").split())
        status = f"🎶 {clean_name}"
        return status[:120]

    async def _set_voice_channel_status(self, channel, station_name: str | None) -> None:
        if not self._is_voice_channel(channel):
            return
        status = self._voice_status_text(station_name)
        try:
            await channel.edit(status=status, reason="Radio: actualizar estado del canal")
        except discord.Forbidden:
            logger.warning(
                "[radio] Sin permiso Set Voice Channel Status en canal %s (%s)",
                getattr(channel, "id", None),
                getattr(getattr(channel, "guild", None), "id", None),
            )
        except discord.HTTPException:
            logger.exception("[radio] Discord rechazó actualizar el status del canal %s", getattr(channel, "id", None))
        except TypeError:
            logger.debug("[radio] Esta versión de discord.py no soporta edit(status=...)")

    async def _clear_voice_channel_status(self, channel) -> None:
        if not self._is_voice_channel(channel):
            return
        try:
            await channel.edit(status=None, reason="Radio: limpiar estado del canal")
        except (discord.Forbidden, discord.HTTPException, TypeError):
            logger.debug(
                "[radio] No se pudo limpiar el status del canal %s",
                getattr(channel, "id", None),
                exc_info=True,
            )

    async def _check_and_connect_guild(
        self,
        guild: discord.Guild,
        *,
        restart_stream: bool = False,
    ):
        """Lógica de conexión/reproducción para un guild específico.

        Puede llamarse directamente desde el API (vía run_coroutine_threadsafe)
        para aplicar cambios inmediatamente sin esperar el tick de 60 s.
        """
        cfg = self.db.get_lofi_config(guild.id)
        logger.debug(
            f"[radio] guild={guild.name} enabled={cfg.get('enabled')} "
            f"channel_id={cfg.get('channel_id')}"
        )

        if not cfg.get("enabled"):
            vc = self._voice_client_for_guild(guild)
            if vc:
                try:
                    await self._clear_voice_channel_status(getattr(vc, "channel", None))
                    await vc.disconnect()
                except Exception:
                    pass
            return

        channel_id = self._coerce_channel_id(cfg.get("channel_id"))
        if not channel_id:
            logger.warning(f"[radio] {guild.name}: radio habilitada pero sin channel_id")
            return

        channel = await self._resolve_voice_channel(guild, channel_id)
        if not self._is_voice_channel(channel):
            logger.warning(
                f"[radio] {guild.name}: channel {channel_id} no encontrado o no es VoiceChannel"
            )
            return

        try:
            async with self._guild_lock(guild.id):
                vc = await self._ensure_voice_client(guild, channel)
        except Exception:
            logger.exception(f"[radio] No se pudo conectar/mover al canal en {guild.name}")
            return

        if restart_stream and vc.is_playing():
            logger.info(f"[radio] Reiniciando stream activo en {guild.name}")
            vc.stop()
            await asyncio.sleep(self._playback_wait)

        if not vc.is_playing():
            logger.info(f"[radio] Iniciando reproducción en {guild.name}")
            self.start_playing(vc, channel, cfg)
        else:
            await self._set_voice_channel_status(channel, cfg.get("station_name"))

    async def connect_guild(self, guild_id: int, *, restart_stream: bool = False):
        """Punto de entrada para el API: conecta/actualiza radio en un guild concreto."""
        if restart_stream:
            await self.restart_guild(guild_id)
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            logger.warning(f"[radio] connect_guild: guild {guild_id} no encontrado en cache")
            return
        await self._check_and_connect_guild(guild, restart_stream=restart_stream)

    async def restart_guild(self, guild_id: int):
        """Reinicia de verdad la radio de un guild tras cambios desde dashboard.

        A diferencia de connect/update, esto fuerza un nuevo proceso FFmpeg:
        para la reproducción actual, desconecta el voice client y vuelve a
        conectar usando la configuración persistida más reciente.
        """
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            logger.warning(f"[radio] restart_guild: guild {guild_id} no encontrado en cache")
            return

        cfg = self.db.get_lofi_config(guild.id)
        if cfg.get("enabled"):
            channel_id = self._coerce_channel_id(cfg.get("channel_id"))
            if not channel_id:
                logger.warning(f"[radio] {guild.name}: restart cancelado, radio habilitada sin channel_id")
                return
            channel = await self._resolve_voice_channel(guild, channel_id)
            if not self._is_voice_channel(channel):
                logger.warning(
                    f"[radio] {guild.name}: restart cancelado, channel {channel_id} no encontrado o no es VoiceChannel"
                )
                return

        self._restart_in_progress.add(guild_id)
        try:
            vc = self._voice_client_for_guild(guild)
            if vc and vc.is_connected():
                old_channel = getattr(vc, "channel", None)
                try:
                    if vc.is_playing() or vc.is_paused():
                        logger.info(f"[radio] Deteniendo stream activo en {guild.name} por cambio de dashboard")
                        vc.stop()
                except Exception:
                    logger.debug("[radio] No se pudo detener stream antes del restart", exc_info=True)

                await asyncio.sleep(self._playback_wait)
                try:
                    await self._clear_voice_channel_status(old_channel)
                    async with self._guild_lock(guild.id):
                        await vc.disconnect(force=True)
                    logger.info(f"[radio] Voice client desconectado en {guild.name} para reinicio limpio")
                except Exception:
                    logger.exception(f"[radio] No se pudo desconectar voice client en {guild.name}")
                    raise

                await asyncio.sleep(0.5)

            await self._check_and_connect_guild(guild, restart_stream=False)
        finally:
            self._restart_in_progress.discard(guild_id)

    def start_playing(self, vc, channel, cfg):
        stream_url = cfg.get("stream_url") or LOFI_STREAM_URL
        station_name = cfg.get("station_name") or "Lofi Radio 24/7"

        try:
            import shutil
            ffmpeg_path = shutil.which("ffmpeg") or "ffmpeg"
            ffmpeg_options = {
                'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
                'options': '-vn'
            }
            audio_source = discord.FFmpegPCMAudio(stream_url, executable=ffmpeg_path, **ffmpeg_options)

            vol = cfg.get("volume", 100) / 100.0
            if vol != 1.0:
                audio_source = discord.PCMVolumeTransformer(audio_source, volume=vol)

            def after_playback(error):
                if error:
                    logger.error(f"Error en reproducción de radio: {error}")
                if channel.guild.id in self._restart_in_progress:
                    logger.debug("[radio] Ignorando auto-reconnect por restart manual en %s", channel.guild.id)
                    return
                # Re-leer config desde DB para tener el estado más actualizado
                current_cfg = self.db.get_lofi_config(channel.guild.id)
                if current_cfg.get("enabled"):
                    asyncio.run_coroutine_threadsafe(
                        self.reconnect_stream(vc, channel, current_cfg), self.bot.loop
                    )

            vc.play(audio_source, after=after_playback)

            # Actualizar el status visible del canal de voz donde reproduce.
            # No cambiamos la presencia/perfil del bot: sólo el estado del voicechat.
            try:
                asyncio.create_task(self._set_voice_channel_status(channel, station_name))
            except RuntimeError:
                asyncio.run_coroutine_threadsafe(
                    self._set_voice_channel_status(channel, station_name),
                    self.bot.loop,
                )
        except Exception:
            logger.exception(f"Error reproduciendo radio en el canal {channel.id}")

    def _resolve_stream_sync(self, url: str) -> str:
        """Resuelve playlists (.m3u/.pls) devolviendo la primera URL directa.

        Método síncrono pensado para ejecutarse en executor y no bloquear el loop.
        """
        try:
            with requests.get(url, stream=True, timeout=5) as r:
                content_type = r.headers.get("content-type", "").lower()
                
                is_playlist = url.lower().endswith(('.m3u', '.m3u8', '.pls')) or 'scpls' in content_type or 'mpegurl' in content_type
                
                # Si no parece una playlist, devolver la URL original y que FFmpeg se encargue
                if not is_playlist:
                    return url
                
                # Leemos solo el primer chunk de 4KB para evitar bloquear la memoria
                chunk = next(r.iter_content(chunk_size=4096, decode_unicode=True), "")
                if isinstance(chunk, bytes):
                    chunk = chunk.decode('utf-8', errors='ignore')
                
                lines = [l.strip() for l in chunk.splitlines() if l and not l.strip().startswith('#') and not l.strip().startswith('[')]
                for line in lines:
                    if line.lower().startswith("file1="):
                        return line.split("=", 1)[1].strip()
                    elif line.startswith("http"):
                        return line
        except Exception:
            pass
        return url

    async def reconnect_stream(self, vc, channel, cfg):
        # Respetar la opción auto_reconnect del dashboard. Default 1 (activo).
        if not cfg.get("auto_reconnect", 1):
            return
        await asyncio.sleep(2)
        if vc and vc.is_connected() and not vc.is_playing() and cfg.get("enabled"):
            # Pausar si el canal está vacío y el admin lo configuró así.
            if cfg.get("pause_on_empty"):
                non_bot_members = [m for m in channel.members if not m.bot]
                if not non_bot_members:
                    return
            self.start_playing(vc, channel, cfg)

    radio_group = app_commands.Group(
        name="radio",
        description="Configuración de Radio 24/7",
        default_permissions=discord.Permissions(administrator=True),
    )

    @radio_group.command(name="status", description="Consulta el estado y configuración actual de la radio")
    async def radio_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cfg = self.db.get_lofi_config(interaction.guild_id)
        if not cfg.get("enabled"):
            return await interaction.followup.send("❌ La radio está desactivada en este servidor.", ephemeral=True)

        canal = self._resolve_interaction_radio_channel(interaction, cfg)
        vc = self._voice_client_for_guild(interaction.guild)

        status_text = "🟢 Reproduciendo" if (vc and vc.is_playing()) else "🔴 Detenido / Conectando..."
        station = cfg.get("station_name", "Lofi Radio 24/7")

        embed = discord.Embed(title="Estado de la Radio", color=discord.Color.blue())
        embed.add_field(name="Estación", value=f"**{station}**", inline=False)
        embed.add_field(name="Canal", value=canal.mention if canal else "No encontrado", inline=True)
        embed.add_field(name="Estado", value=status_text, inline=True)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @radio_group.command(name="restart", description="Fuerza la reconexión y reinicio del stream de radio")
    @app_commands.checks.has_permissions(administrator=True)
    async def radio_restart(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cfg = self.db.get_lofi_config(interaction.guild_id)
        if not cfg.get("enabled"):
            return await interaction.followup.send("❌ La radio no está activada. Actívala desde el panel web.", ephemeral=True)

        channel = self._resolve_interaction_radio_channel(
            interaction,
            cfg,
            persist_fallback=True,
        )
        if not channel:
            return await interaction.followup.send(
                "❌ Canal de voz no encontrado. Entra a un canal de voz o configúralo desde el panel web.",
                ephemeral=True,
            )

        vc = self._voice_client_for_guild(interaction.guild)
        try:
            if vc and vc.is_connected():
                if vc.is_playing():
                    vc.stop()
                if vc.channel.id != channel.id:
                    await vc.move_to(channel)
            else:
                vc = await self._ensure_voice_client(interaction.guild, channel)

            await asyncio.sleep(self._playback_wait)
            self.start_playing(vc, channel, cfg)
            station = cfg.get("station_name", "Lofi Radio 24/7")
            await interaction.followup.send(f"🔄 Stream reiniciado — **{station}**", ephemeral=True)
        except Exception as e:
            logger.exception(f"Error en /radio restart en {interaction.guild.name}")
            await interaction.followup.send(f"❌ Error al reiniciar el stream: {e}", ephemeral=True)

    @radio_group.command(name="buscar", description="Busca estaciones de radio globales para reproducir")
    @app_commands.describe(nombre="Nombre, género o país a buscar")
    @app_commands.checks.has_permissions(administrator=True)
    async def radio_search(self, interaction: discord.Interaction, nombre: str):
        await interaction.response.defer(ephemeral=True)

        try:
            params = {
                "name": nombre,
                "limit": 10,
                "hidebroken": "true",
                "order": "clickcount",
                "reverse": "true",
            }
            data = []
            # Lista de mirrors de radio-browser (rotación oficial). Si el primero
            # falla o devuelve vacío, probamos los siguientes — el bug reportado
            # era que con la radio activa la consulta a `de1` colgaba/timeout
            # silenciosamente y no se mostraban resultados.
            mirrors = [
                RADIO_API_URL,
                "https://de2.api.radio-browser.info/json/stations/search",
                "https://fi1.api.radio-browser.info/json/stations/search",
                "https://nl1.api.radio-browser.info/json/stations/search",
            ]
            for url in mirrors:
                try:
                    resp = await asyncio.to_thread(
                        requests.get, url, params=params, timeout=12,
                        headers={"User-Agent": "CatsBot/2.0 (radio-search)"},
                    )
                    resp.raise_for_status()
                    parsed = resp.json()
                    if isinstance(parsed, list) and parsed:
                        data = parsed
                        break
                except Exception as e:
                    logger.debug(f"Mirror {url} falló: {e}")
                    continue

            if not data:
                return await interaction.followup.send(
                    "❌ No se encontraron estaciones con ese nombre. "
                    "Si la radio está activa, espera unos segundos e inténtalo de nuevo.",
                    ephemeral=True,
                )

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
                    try:
                        await inter.response.defer(ephemeral=True)
                    except Exception:
                        pass

                    idx = int(self.values[0])
                    station = self.stations[idx]
                    url = station.get("url_resolved") or station.get("url")
                    name = station.get("name", "Desconocida")

                    # Resolver playlists fuera del loop
                    try:
                        loop = asyncio.get_running_loop()
                        resolved = await loop.run_in_executor(None, self.cog._resolve_stream_sync, url)
                    except Exception:
                        resolved = url

                    # Asegurar columnas y actualizar configuración
                    try:
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

                    cfg = self.db.get_lofi_config(inter.guild_id)
                    channel = self.cog._resolve_interaction_radio_channel(
                        inter,
                        cfg,
                        persist_fallback=True,
                    )
                    vc = self.cog._voice_client_for_guild(inter.guild)

                    if not channel:
                        await inter.followup.send(
                            "❌ Canal de voz no encontrado. Entra a un canal de voz o configúralo desde el panel web.",
                            ephemeral=True,
                        )
                        try:
                            await inter.message.edit(view=None)
                        except Exception:
                            pass
                        return

                    try:
                        if not vc or not vc.is_connected():
                            vc = await self.cog._ensure_voice_client(inter.guild, channel)
                            await asyncio.sleep(self.cog._playback_wait)
                            self.cog.start_playing(vc, channel, cfg)
                        else:
                            # Cambio en caliente: detener el stream actual,
                            # mover de canal si hace falta y SIEMPRE relanzar
                            # con la nueva URL/estación.
                            if vc.is_playing():
                                vc.stop()

                            if vc.channel.id != channel.id:
                                try:
                                    await vc.move_to(channel)
                                except Exception:
                                    pass

                            await asyncio.sleep(self.cog._playback_wait)
                            self.cog.start_playing(vc, channel, cfg)

                    except Exception as e:
                        logger.exception(f"Error aplicando emisora en {inter.guild.name}: {e}")
                        await inter.followup.send(f"❌ Error al aplicar la emisora: {e}", ephemeral=True)
                        try:
                            await inter.message.edit(view=None)
                        except Exception:
                            pass
                        return

                    try:
                        await inter.followup.send(f"📻 **Radio cambiada a:** {name}\nSe aplicará en unos segundos.", ephemeral=True)
                        try:
                            await inter.message.edit(view=None)
                        except Exception:
                            pass
                    except Exception:
                        pass

            view = discord.ui.View()
            view.add_item(RadioSelect(data, self.db, self.bot, self))

            await interaction.followup.send("Selecciona la estación que deseas sintonizar:", view=view)

        except Exception as e:
            logger.error(f"Error en radio_search: {e}")
            await interaction.followup.send(f"❌ Error consultando la API de radio: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Radio(bot))
