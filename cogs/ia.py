import os
import logging
import asyncio
import time
import random
import re
from time import monotonic
from datetime import datetime, timezone
from typing import Optional, List, Dict

import discord
from discord.ext import commands
from discord import app_commands

try:
    from google import genai
    from google.genai import types
    from google.genai import errors as genai_errors
except ImportError:
    genai = None
    types = None
    genai_errors = None

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

        # Rate limiting / concurrency controls
        # Tokens per period (capacity) and period in seconds
        self._rate_capacity = int(os.getenv("AI_RATE_CAPACITY", "12"))
        self._rate_period = float(os.getenv("AI_RATE_PERIOD", "60"))
        self._rate_tokens = float(self._rate_capacity)
        self._rate_last = monotonic()
        self._rate_lock = asyncio.Lock()

        # Number of concurrent requests allowed to the API
        self._ai_concurrency = int(os.getenv("AI_CONCURRENCY", "2"))
        self._ai_semaphore = asyncio.Semaphore(self._ai_concurrency)

        # Queue + workers to smooth bursts
        self._ai_queue = asyncio.Queue(maxsize=int(os.getenv("AI_QUEUE_SIZE", "64")))
        self._worker_tasks: List[asyncio.Task] = []
        for i in range(self._ai_concurrency):
            task = self.bot.loop.create_task(self._ai_worker(i))
            self._worker_tasks.append(task)

        # Per-user cooldown to avoid floods
        self._last_request: Dict[int, float] = {}
        self._min_user_interval = float(os.getenv("AI_MIN_USER_INTERVAL", "3.0"))

        # Per-model backoff (model -> monotonic timestamp until which model is paused)
        self._model_backoff_until: Dict[str, float] = {}
        self._global_backoff_until: float = 0.0

        # Token estimation: approximate number of characters per token
        self._chars_per_token = int(os.getenv("AI_CHARS_PER_TOKEN", "4"))

        # Metrics (in-memory)
        self._metrics = {
            'requests': 0,
            'success': 0,
            'retries': 0,
            'errors_429': 0,
            'errors_503': 0,
            'fallbacks': 0,
            'queue_max': 0,
            'total_latency': 0.0,
            'latency_count': 0,
            'total_tokens': 0,
        }

    def _inc_metric(self, name: str, value: int = 1) -> None:
        try:
            if name in self._metrics:
                self._metrics[name] += value
        except Exception:
            pass

    def _record_latency(self, seconds: float) -> None:
        try:
            self._metrics['total_latency'] += seconds
            self._metrics['latency_count'] += 1
        except Exception:
            pass

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

    @app_commands.command(name="ai_status", description="Muestra métricas y estado del servicio IA")
    @app_commands.checks.has_permissions(administrator=True)
    async def ai_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        qsize = self._ai_queue.qsize() if hasattr(self, '_ai_queue') else 0
        avg_latency = 0.0
        if self._metrics.get('latency_count', 0) > 0:
            avg_latency = self._metrics['total_latency'] / max(1, self._metrics['latency_count'])

        embed = discord.Embed(title="📊 Estado IA", color=discord.Color.blue())
        embed.add_field(name="Requests en cola", value=f"{qsize}", inline=True)
        embed.add_field(name="Máx cola registrada", value=f"{self._metrics.get('queue_max', 0)}", inline=True)
        embed.add_field(name="Requests totales", value=f"{self._metrics.get('requests', 0)}", inline=True)
        embed.add_field(name="Respuestas OK", value=f"{self._metrics.get('success', 0)}", inline=True)
        embed.add_field(name="Reintentos", value=f"{self._metrics.get('retries', 0)}", inline=True)
        embed.add_field(name="Fallbacks usados", value=f"{self._metrics.get('fallbacks', 0)}", inline=True)
        embed.add_field(name="Errores 429", value=f"{self._metrics.get('errors_429', 0)}", inline=True)
        embed.add_field(name="Errores 503", value=f"{self._metrics.get('errors_503', 0)}", inline=True)
        embed.add_field(name="Tokens totales estimados", value=f"{self._metrics.get('total_tokens', 0)}", inline=True)
        embed.add_field(name="Latency avg (s)", value=f"{avg_latency:.2f}", inline=True)
        embed.add_field(name="Concurrency", value=f"{self._ai_concurrency}", inline=True)
        embed.add_field(name="Rate (cap/period)", value=f"{self._rate_capacity}/{self._rate_period}s", inline=True)

        # Backoff info
        backoff_text = []
        now = monotonic()
        for model, until in self._model_backoff_until.items():
            if until > now:
                backoff_text.append(f"{model}: {int(until-now)}s")
        if self._global_backoff_until and self._global_backoff_until > now:
            backoff_text.append(f"GLOBAL: {int(self._global_backoff_until-now)}s")

        embed.add_field(name="Model Backoffs", value=("\n".join(backoff_text) if backoff_text else "None"), inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    def _get_system_prompt(self, guild_id: int) -> str:
        base_prompt = os.getenv("AI_SYSTEM_PROMPT", "Eres un asistente amigable y útil llamado TortuguBot. Si te hacen preguntas de actualidad, usa Google Search para responder.")
        srv_ctx = self.server_contexts.get(guild_id, "")
        if srv_ctx:
            base_prompt += f"\n\nContexto actual del servidor donde te encuentras:\n{srv_ctx}"
        return base_prompt

    async def _generate_content_with_retries(
        self,
        model,
        contents,
        config,
        retries: int = 3,
        initial_backoff: float = 1.0,
        fallback_models: Optional[List[str]] = None,
    ):
        """Intentar generar contenido con reintentos exponenciales y modelos de fallback.

        Ejecuta la llamada bloqueante en un hilo (`asyncio.to_thread`) para no
        bloquear el loop de eventos.
        """
        if fallback_models is None:
            fallback_models = []

        last_exc: Optional[BaseException] = None

        def _parse_retry_delay_from_error(err) -> Optional[float]:
            # Try to extract RetryInfo.retryDelay (seconds) from error details
            try:
                details = getattr(err, 'details', None)
                if not details:
                    return None

                def _walk(d):
                    if isinstance(d, dict):
                        # direct 'retryDelay' or nested 'error'->'details'
                        if 'retryDelay' in d:
                            val = d.get('retryDelay')
                            if isinstance(val, str) and val.endswith('s'):
                                try:
                                    return float(val[:-1])
                                except Exception:
                                    return None
                            try:
                                return float(val)
                            except Exception:
                                return None
                        if 'details' in d:
                            return _walk(d['details'])
                        if 'error' in d:
                            return _walk(d['error'].get('details', d['error']))
                    if isinstance(d, list):
                        for it in d:
                            if isinstance(it, dict):
                                t = it.get('@type', '')
                                if 'RetryInfo' in t or 'retryinfo' in t.lower():
                                    rd = it.get('retryDelay')
                                    if isinstance(rd, str) and rd.endswith('s'):
                                        try:
                                            return float(rd[:-1])
                                        except Exception:
                                            continue
                                    if isinstance(rd, dict) and 'seconds' in rd:
                                        try:
                                            return float(rd.get('seconds', 0))
                                        except Exception:
                                            continue
                                # fallback inspect nested
                                sub = _walk(it.get('details', it))
                                if sub:
                                    return sub
                    return None

                return _walk(details)
            except Exception:
                return None

        async def _acquire_tokens(tokens_needed: int, max_wait: float = 30.0) -> bool:
            start = monotonic()
            while True:
                async with self._rate_lock:
                    now = monotonic()
                    elapsed = now - self._rate_last
                    if elapsed > 0:
                        refill = (elapsed / self._rate_period) * self._rate_capacity
                        if refill > 0:
                            self._rate_tokens = min(self._rate_capacity, self._rate_tokens + refill)
                            self._rate_last = now
                    if self._rate_tokens >= tokens_needed:
                        self._rate_tokens -= tokens_needed
                        return True
                await asyncio.sleep(0.5)
                if monotonic() - start > max_wait:
                    return False

        # Try to perform the call with concurrency control and rate limiting
        for attempt in range(1, retries + 1):
            # Estimar tokens necesarios según el contenido
            try:
                tokens_needed = self._estimate_input_tokens(contents)
            except Exception:
                tokens_needed = 1

            # Esperar a tener tokens de rate
            have = await _acquire_tokens(tokens_needed)
            if not have:
                # No pudimos obtener tokens en tiempo razonable
                last_exc = RuntimeError("Timeout esperando cuota de rate limit local")
                raise last_exc

            # Controlar concurrencia
            async with self._ai_semaphore:
                try:
                    response = await asyncio.to_thread(
                        self.client.models.generate_content,
                        model=model,
                        contents=contents,
                        config=config,
                    )
                    # Metrics: success and token accounting
                    try:
                        self._inc_metric('success', 1)
                        self._inc_metric('total_tokens', tokens_needed if 'tokens_needed' in locals() else 1)
                    except Exception:
                        pass
                    return response
                except Exception as e:  # noqa: BLE001 - manejamos varias excepciones
                    last_exc = e

                    # Detectar errores temporales (503 / UNAVAILABLE) y 429 (quota)
                    retriable = False
                    retry_delay_override: Optional[float] = None
                    if genai_errors is not None and isinstance(e, genai_errors.ServerError):
                        retriable = True
                    else:
                        msg = str(e).lower()
                        if "503" in msg or "unavailable" in msg or "high demand" in msg:
                            retriable = True
                        # Buscar RetryInfo para 429 con suggestion
                        m = re.search(r'retry in (\d+\.?\d*)s', msg)
                        if m:
                            try:
                                retry_delay_override = float(m.group(1))
                            except Exception:
                                pass

                    if attempt < retries and retriable:
                        # Metrics: record a retry
                        self._inc_metric('retries', 1)
                        # Exponential backoff + jitter
                        base = initial_backoff * (2 ** (attempt - 1))
                        jitter = random.uniform(0, base * 0.1)
                        wait = retry_delay_override if retry_delay_override is not None else (base + jitter)
                        await asyncio.sleep(wait)
                        continue

                    # Si es ClientError (por ejemplo 429) intentamos extraer RetryInfo
                    if genai_errors is not None and isinstance(e, genai_errors.ClientError):
                        # 429 / quota exceeded
                        self._inc_metric('errors_429', 1)
                        rd = _parse_retry_delay_from_error(e)
                        if rd:
                            # Marcar modelo en backoff para evitar usarlo hasta pasado rd
                            try:
                                self._model_backoff_until[model] = monotonic() + rd
                            except Exception:
                                pass
                            # Si existe un fallback, probarlo; si no, esperar rd y reintentar
                            if attempt < retries:
                                await asyncio.sleep(rd)
                                continue
                    # count server errors (503)
                    if genai_errors is not None and isinstance(e, genai_errors.ServerError):
                        self._inc_metric('errors_503', 1)

                    # Intentar modelos de fallback si se proporcionan (sin reintentos largos)
                    if fallback_models:
                        for fb in fallback_models:
                            try:
                                # small delay before fallback
                                await asyncio.sleep(0.5)
                                response = await asyncio.to_thread(
                                    self.client.models.generate_content,
                                    model=fb,
                                    contents=contents,
                                    config=config,
                                )
                                # Metrics: fallback used
                                self._inc_metric('fallbacks', 1)
                                return response
                            except Exception as e_fb:
                                last_exc = e_fb
                                continue

                    # Si llegamos aquí, re-lanzamos la última excepción
                    raise last_exc

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

    def _estimate_input_tokens(self, contents) -> int:
        try:
            total_chars = 0
            for content in contents:
                parts = getattr(content, 'parts', None)
                if parts:
                    for p in parts:
                        txt = getattr(p, 'text', None)
                        if txt:
                            total_chars += len(str(txt))
            tokens = max(1, int(total_chars / max(1, self._chars_per_token)))
            return tokens
        except Exception:
            return 1

    async def _ai_worker(self, worker_id: int):
        logger.info(f"IA worker {worker_id} iniciado")
        while True:
            job = await self._ai_queue.get()
            try:
                await self._process_job(job)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error en worker IA {worker_id}: {e}", exc_info=True)
            finally:
                try:
                    self._ai_queue.task_done()
                except Exception:
                    pass

    async def _process_job(self, job: dict):
        message: discord.Message = job.get('message')
        ctx_id: str = job.get('ctx_id')
        contents = job.get('contents')
        config = job.get('config')
        fallback = job.get('fallback', [])
        is_ai_channel = job.get('is_ai_channel', False)

        # Global backoff check
        now = monotonic()
        if self._global_backoff_until and now < self._global_backoff_until:
            retry_after = int(self._global_backoff_until - now)
            try:
                await message.channel.send(f"❌ IA en mantenimiento temporal, inténtalo en {retry_after}s.")
            except Exception:
                pass
            return

        try:
            async with message.channel.typing():
                # Elegir modelo disponible (si el primario está en backoff, usar fallback)
                model = self.db.get_ai_config(message.guild.id).get('ai_model', 'gemini-2.5-flash')
                models_order = [model] + fallback
                chosen = None
                for m in models_order:
                    until = self._model_backoff_until.get(m, 0)
                    if until < monotonic():
                        chosen = m
                        break
                if not chosen:
                    # No hay modelos disponibles ahora mismo
                    await message.channel.send("❌ Todos los modelos están temporalmente limitados. Intenta de nuevo más tarde.")
                    return

                start_time = monotonic()
                response = await self._generate_content_with_retries(
                    model=chosen,
                    contents=contents,
                    config=config,
                    retries=3,
                    initial_backoff=1.0,
                    fallback_models=[m for m in models_order if m != chosen],
                )
                # Record latency for the request
                try:
                    latency = monotonic() - start_time
                    self._record_latency(latency)
                except Exception:
                    pass

                reply_text = getattr(response, 'text', None) or "No pude generar una respuesta."
                try:
                    # compute latency if available: we used monotonic before/after only here
                    # Note: if the caller wants more precise timing, can be added in _generate_content_with_retries
                    if now_after:
                        latency = 0.0
                        try:
                            # attempt to get start time stored in job (not present) — fallback to 0
                            latency = 0.0
                        except Exception:
                            latency = 0.0
                        # record the measured latency as a rough sample using response timing
                        # We don't have start timestamp here; instead rely on total_latency updated elsewhere
                        pass
                except Exception:
                    pass

                # Añadir respuesta al historial real si aún existe
                if ctx_id in self.chat_histories:
                    try:
                        self.chat_histories[ctx_id].append(
                            types.Content(role="model", parts=[types.Part.from_text(text=reply_text)])
                        )
                        if len(self.chat_histories[ctx_id]) > 20:
                            self.chat_histories[ctx_id] = self.chat_histories[ctx_id][-20:]
                    except Exception:
                        pass

                # Enviar respuesta
                if is_ai_channel and isinstance(message.channel, discord.TextChannel):
                    await self._send_via_webhook(message.channel, message.author, reply_text)
                else:
                    if len(reply_text) > 2000:
                        reply_text = reply_text[:1996] + "..."
                    await message.reply(reply_text)
        except Exception as e:
            logger.error(f"Error procesando job IA: {e}", exc_info=True)
            try:
                await message.channel.send("❌ Error al procesar la petición de IA.")
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignorar mensajes de bots, webhooks o si client no está configurado
        if message.author.bot or getattr(message, 'webhook_id', None) or not self.client or not message.guild:
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

            # Encolar la petición para evitar picos y manejar rate-limits
            try:
                now = monotonic()
                last = self._last_request.get(message.author.id, 0)
                if now - last < self._min_user_interval:
                    retry_after = int(self._min_user_interval - (now - last))
                    try:
                        await message.reply(f"⏳ Por favor espera {retry_after}s antes de otra consulta de IA.")
                    except Exception:
                        pass
                    return

                # Preparar configuración y snapshot del historial
                model = cfg.get("ai_model", "gemini-2.5-flash")
                sys_prompt = self._get_system_prompt(message.guild.id)
                config = types.GenerateContentConfig(
                    system_instruction=sys_prompt,
                    temperature=0.7,
                    tools=[{"google_search": {}}],
                )

                fallback = ["gemini-2.5-pro"] if "flash" in model else ["gemini-2.5-flash"]

                contents_snapshot = list(self.chat_histories[ctx_id])

                job = {
                    'message': message,
                    'ctx_id': ctx_id,
                    'contents': contents_snapshot,
                    'config': config,
                    'fallback': fallback,
                    'is_ai_channel': is_ai_channel,
                }

                try:
                    self._ai_queue.put_nowait(job)
                    self._last_request[message.author.id] = now
                    # Metrics: new request and track queue max
                    self._inc_metric('requests', 1)
                    qsz = self._ai_queue.qsize()
                    if qsz > self._metrics.get('queue_max', 0):
                        self._metrics['queue_max'] = qsz
                except asyncio.QueueFull:
                    try:
                        await message.reply("❌ Servidor de IA ocupado, inténtalo en unos segundos.")
                    except Exception:
                        pass
                return
            except Exception as e:
                logger.error(f"Error en IA al encolar: {e}", exc_info=True)
                try:
                    await message.channel.send("❌ Error al procesar la petición de IA.")
                except Exception:
                    pass

async def setup(bot: commands.Bot):
    await bot.add_cog(IA(bot))
