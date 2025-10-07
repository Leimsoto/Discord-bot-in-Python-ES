# Importamos las librerías necesarias
import os
import discord
import sqlite3
import asyncio
import logging
import google.generativeai as genai

from discord.ui import View, Button
from pathlib import Path
from contextlib import contextmanager
from typing import Optional, Dict, Tuple

from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv
from mantener_vivo import mantener_vivo
from datetime import datetime, timedelta, timezone

# ----------------Configuración de Logging----------------#

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('DiscordBot')

# ----------------Base de datos----------------#
BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / 'warns_de_usuario.db'

@contextmanager
def get_db_connection():
    """Context manager para conexiones de base de datos"""
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Error en la base de datos: {e}")
        raise
    finally:
        conn.close()

def create_based():
    """Crea la base de datos y las tablas necesarias con índices optimizados"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Tabla de usuarios
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users_per_guild (
                user_id INTEGER,
                warns INTEGER DEFAULT 0,
                guild_id INTEGER,
                mute_start TEXT,
                mute_duration INTEGER,
                PRIMARY KEY(user_id, guild_id)
            )
        ''')
        
        # Tabla de logs de moderación
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS mod_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                guild_id INTEGER,
                action_type TEXT,
                moderator_id INTEGER,
                reason TEXT,
                timestamp TEXT
            )
        ''')
        
        # Tabla de lockdowns
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS lockdowns (
                channel_id INTEGER,
                guild_id INTEGER,
                lockdown_start TEXT,
                lockdown_duration INTEGER,
                original_permissions TEXT,
                PRIMARY KEY(channel_id, guild_id)
            )
        ''')
        
        # Tabla de configuración por servidor
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id INTEGER PRIMARY KEY,
                muted_role_id INTEGER,
                log_channel_id INTEGER,
                max_warns INTEGER DEFAULT 5,
                auto_mute_duration INTEGER DEFAULT 43200
            )
        ''')
        
        # Crear índices para optimización
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_guild ON users_per_guild(user_id, guild_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_mute_active ON users_per_guild(mute_start) WHERE mute_start IS NOT NULL')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_lockdown_active ON lockdowns(lockdown_start) WHERE lockdown_start IS NOT NULL')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_mod_logs_guild ON mod_logs(guild_id, timestamp)')
        
        logger.info("Base de datos inicializada correctamente")

create_based()

# ----------------Funciones de Base de Datos----------------#

def get_warns(user_id: int, guild_id: int) -> int:
    """Obtiene y actualiza los warns de un usuario de forma segura"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT warns FROM users_per_guild
                WHERE user_id = ? AND guild_id = ?;
            """, (user_id, guild_id))
            
            result = cursor.fetchone()
            
            if result is None:
                cursor.execute("""
                    INSERT INTO users_per_guild (user_id, warns, guild_id)
                    VALUES (?, 1, ?);
                """, (user_id, guild_id))
                return 1
            
            new_warns = result[0] + 1
            cursor.execute("""
                UPDATE users_per_guild
                SET warns = ?
                WHERE user_id = ? AND guild_id = ?;
            """, (new_warns, user_id, guild_id))
            
            return new_warns
    except Exception as e:
        logger.error(f"Error al obtener warns: {e}")
        raise

def get_user_warns_count(user_id: int, guild_id: int) -> int:
    """Consulta los warns sin modificar"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT warns FROM users_per_guild
                WHERE user_id = ? AND guild_id = ?;
            """, (user_id, guild_id))
            
            result = cursor.fetchone()
            return result[0] if result else 0
    except Exception as e:
        logger.error(f"Error al consultar warns: {e}")
        return 0

def reset_warns(user_id: int, guild_id: int) -> bool:
    """Resetea los warns de un usuario"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE users_per_guild
                SET warns = 0
                WHERE user_id = ? AND guild_id = ?;
            """, (user_id, guild_id))
            return True
    except Exception as e:
        logger.error(f"Error al resetear warns: {e}")
        return False

def clear_mute_data(user_id: int, guild_id: int) -> bool:
    """Limpia los datos de mute de un usuario"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE users_per_guild
                SET mute_start = NULL, mute_duration = NULL
                WHERE user_id = ? AND guild_id = ?;
            """, (user_id, guild_id))
            return True
    except Exception as e:
        logger.error(f"Error al limpiar mute: {e}")
        return False

def get_mute_info(user_id: int, guild_id: int) -> Optional[Dict]:
    """Obtiene información sobre el mute de un usuario"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT mute_start, mute_duration
                FROM users_per_guild
                WHERE user_id = ? AND guild_id = ?;
            """, (user_id, guild_id))
            
            result = cursor.fetchone()
            
            if result and result[0]:
                return {
                    'mute_start': result[0],
                    'mute_duration': result[1]
                }
            return None
    except Exception as e:
        logger.error(f"Error al obtener info de mute: {e}")
        return None

def set_mute(user_id: int, guild_id: int, duration: Optional[int] = None) -> bool:
    """Establece un mute para un usuario"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT user_id FROM users_per_guild
                WHERE user_id = ? AND guild_id = ?;
            """, (user_id, guild_id))
            
            if cursor.fetchone() is None:
                cursor.execute("""
                    INSERT INTO users_per_guild (user_id, warns, guild_id, mute_start, mute_duration)
                    VALUES (?, 0, ?, ?, ?);
                """, (user_id, guild_id, datetime.now(timezone.utc).isoformat(), duration))
            else:
                cursor.execute("""
                    UPDATE users_per_guild
                    SET mute_start = ?, mute_duration = ?
                    WHERE user_id = ? AND guild_id = ?;
                """, (datetime.now(timezone.utc).isoformat(), duration, user_id, guild_id))
            return True
    except Exception as e:
        logger.error(f"Error al establecer mute: {e}")
        return False

def parse_time(time_str: str) -> Optional[int]:
    """Convierte una cadena de tiempo a segundos"""
    time_str = time_str.lower().strip()
    
    if not time_str:
        return None
    
    try:
        if time_str[-1] in ['s', 'm', 'h', 'd', 'w']:
            number = int(time_str[:-1])
            unit = time_str[-1]
            
            if unit == 's':
                return number
            elif unit == 'm':
                return number * 60
            elif unit == 'h':
                return number * 3600
            elif unit == 'd':
                return number * 86400
            elif unit == 'w':
                return number * 604800
        else:
            return int(time_str) * 60
    except ValueError:
        return None
    
    return None

def format_duration(seconds: int) -> str:
    """Formatea segundos en un formato legible"""
    if seconds is None:
        return "Permanente"
    
    weeks = seconds // 604800
    days = (seconds % 604800) // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    
    parts = []
    if weeks > 0:
        parts.append(f"{weeks}sem")
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 and not parts:
        parts.append(f"{secs}s")
    
    return " ".join(parts) if parts else "0s"

def log_action(user_id: int, guild_id: int, action_type: str, moderator_id: int, reason: str) -> bool:
    """Registra una acción de moderación"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO mod_logs (user_id, guild_id, action_type, moderator_id, reason, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, guild_id, action_type, moderator_id, reason, datetime.now(timezone.utc).isoformat()))
            return True
    except Exception as e:
        logger.error(f"Error al registrar acción: {e}")
        return False

def set_lockdown(channel_id: int, guild_id: int, duration: Optional[int] = None, original_permissions: Optional[str] = None) -> bool:
    """Registra un lockdown en la base de datos"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO lockdowns (channel_id, guild_id, lockdown_start, lockdown_duration, original_permissions)
                VALUES (?, ?, ?, ?, ?)
            """, (channel_id, guild_id, datetime.now(timezone.utc).isoformat(), duration, original_permissions))
            return True
    except Exception as e:
        logger.error(f"Error al establecer lockdown: {e}")
        return False

def get_lockdown_info(channel_id: int, guild_id: int) -> Optional[Dict]:
    """Obtiene información sobre el lockdown de un canal"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT lockdown_start, lockdown_duration, original_permissions
                FROM lockdowns
                WHERE channel_id = ? AND guild_id = ?
            """, (channel_id, guild_id))
            
            result = cursor.fetchone()
            
            if result:
                return {
                    'lockdown_start': result[0],
                    'lockdown_duration': result[1],
                    'original_permissions': result[2]
                }
            return None
    except Exception as e:
        logger.error(f"Error al obtener info de lockdown: {e}")
        return None

def clear_lockdown(channel_id: int, guild_id: int) -> bool:
    """Elimina el registro de lockdown de un canal"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM lockdowns
                WHERE channel_id = ? AND guild_id = ?
            """, (channel_id, guild_id))
            return True
    except Exception as e:
        logger.error(f"Error al limpiar lockdown: {e}")
        return False

def get_guild_config(guild_id: int) -> Dict:
    """Obtiene la configuración de un servidor"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT muted_role_id, log_channel_id, max_warns, auto_mute_duration
                FROM guild_config
                WHERE guild_id = ?
            """, (guild_id,))
            
            result = cursor.fetchone()
            
            if result:
                return {
                    'muted_role_id': result[0],
                    'log_channel_id': result[1],
                    'max_warns': result[2],
                    'auto_mute_duration': result[3]
                }
            else:
                # Valores por defecto
                return {
                    'muted_role_id': None,
                    'log_channel_id': None,
                    'max_warns': 5,
                    'auto_mute_duration': 43200
                }
    except Exception as e:
        logger.error(f"Error al obtener configuración: {e}")
        return {
            'muted_role_id': None,
            'log_channel_id': None,
            'max_warns': 5,
            'auto_mute_duration': 43200
        }

# ------------------Bot------------------#

load_dotenv()
token = os.getenv("token")

if not token:
    logger.critical("❌ No se encontró el token en el archivo .env")
    raise ValueError("❌ No se encontró el token en el archivo .env")

mantener_vivo()

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.members = True
intents.guilds = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

SYSTEM_PROMPT = (
    "Eres una IA simpática, útil y con humor ligero. "
    "Responde de forma clara, amistosa y con emojis cuando sea apropiado. "
    "No uses lenguaje ofensivo ni hables de temas inapropiados."
    "Estas en el servidor las tortuguitas de ezku, de un youtuber llamado ezku claramente, trata sobre linux, juegos, memes y social sempre responde teniendo de referencia esto, usando para referirse a un usuario el termino tortuguita"
)

active_chat_channels = set()

# ------------------Funciones auxiliares------------------#

async def get_or_create_muted_role(guild: discord.Guild) -> Optional[discord.Role]:
    """Obtiene o crea el rol de silenciado"""
    muted_role = discord.utils.get(guild.roles, name="Silenciado")
    
    if not muted_role:
        try:
            muted_role = await guild.create_role(
                name="Silenciado",
                permissions=discord.Permissions(send_messages=False, speak=False),
                color=discord.Color.dark_gray(),
                reason="Rol automático para sistema de moderación"
            )
            logger.info(f"Rol 'Silenciado' creado en {guild.name}")
        except discord.Forbidden:
            logger.error(f"Sin permisos para crear rol en {guild.name}")
            return None
        except Exception as e:
            logger.error(f"Error al crear rol: {e}")
            return None
    
    return muted_role

async def setup_muted_role_permissions(guild: discord.Guild, muted_role: discord.Role):
    """Configura permisos del rol silenciado en todos los canales de forma asíncrona"""
    for channel in guild.channels:
        try:
            await channel.set_permissions(
                muted_role,
                send_messages=False,
                speak=False,
                add_reactions=False,
                create_public_threads=False,
                send_messages_in_threads=False
            )
            await asyncio.sleep(0.5)  # Rate limit protection
        except discord.Forbidden:
            logger.warning(f"Sin permisos para configurar {channel.name} en {guild.name}")
        except Exception as e:
            logger.error(f"Error configurando permisos en {channel.name}: {e}")

async def send_log(guild: discord.Guild, embed: discord.Embed):
    """Envía un log al canal configurado del servidor"""
    config = get_guild_config(guild.id)
    if config['log_channel_id']:
        channel = guild.get_channel(config['log_channel_id'])
        if channel and isinstance(channel, discord.TextChannel):
            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                logger.warning(f"Sin permisos para enviar logs en {guild.name}")
            except Exception as e:
                logger.error(f"Error enviando log: {e}")

def has_higher_role(user1: discord.Member, user2: discord.Member) -> bool:
    """Verifica si user1 tiene un rol más alto que user2"""
    return user1.top_role > user2.top_role

# ------------------Eventos------------------#

@client.event
async def on_ready():
    logger.info(f"✅ {client.user.name} se ha conectado a Discord")
    logger.info(f"📊 Conectado a {len(client.guilds)} servidores")
    
    try:
        synced = await tree.sync()
        logger.info(f"🔄 {len(synced)} comandos sincronizados")
    except Exception as e:
        logger.error(f"⚠️ Error al sincronizar comandos: {e}")
    
    if not check_mutes.is_running():
        check_mutes.start()
        logger.info("⏰ Sistema de mutes automáticos iniciado")
    
    if not check_lockdowns.is_running():
        check_lockdowns.start()
        logger.info("🔒 Sistema de lockdowns automáticos iniciado")

@client.event
async def on_guild_join(guild: discord.Guild):
    """Evento cuando el bot se une a un servidor nuevo"""
    logger.info(f"🎉 Bot añadido a nuevo servidor: {guild.name} (ID: {guild.id})")
    
    # Crear rol de silenciado automáticamente
    muted_role = await get_or_create_muted_role(guild)
    if muted_role:
        await setup_muted_role_permissions(guild, muted_role)

@client.event
async def on_guild_channel_create(channel):
    """Configura permisos del rol silenciado en canales nuevos"""
    muted_role = discord.utils.get(channel.guild.roles, name="Silenciado")
    if muted_role:
        try:
            await channel.set_permissions(
                muted_role,
                send_messages=False,
                speak=False,
                add_reactions=False,
                create_public_threads=False,
                send_messages_in_threads=False
            )
            logger.info(f"Permisos configurados en nuevo canal: {channel.name}")
        except Exception as e:
            logger.error(f"Error configurando nuevo canal: {e}")

@client.event
async def on_error(event, *args, **kwargs):
    """Maneja errores globales del bot"""
    logger.error(f"Error en evento {event}", exc_info=True)

# -----------------Tareas programadas-----------------#

@tasks.loop(minutes=1)
async def check_mutes():
    """Revisa cada minuto si ya pasaron las horas de mute"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT user_id, guild_id, mute_start, mute_duration 
                FROM users_per_guild
                WHERE mute_start IS NOT NULL AND mute_duration IS NOT NULL
            """)
            rows = cursor.fetchall()

        for user_id, guild_id, mute_start, mute_duration in rows:
            try:
                guild = client.get_guild(guild_id)
                if not guild:
                    continue

                user = guild.get_member(user_id)
                if not user:
                    continue

                muted_role = discord.utils.get(guild.roles, name="Silenciado")
                if not muted_role:
                    continue

                mute_start_dt = datetime.fromisoformat(mute_start)
                current_time = datetime.now(timezone.utc)
                
                if current_time > mute_start_dt + timedelta(seconds=mute_duration):
                    await user.remove_roles(muted_role, reason="Mute expirado automáticamente")
                    clear_mute_data(user_id, guild_id)
                    
                    logger.info(f"✅ {user.name} desmuteado automáticamente en {guild.name}")
                    
                    # Notificar al usuario
                    try:
                        embed = discord.Embed(
                            title="🔓 Mute Expirado",
                            description=f"Tu mute en **{guild.name}** ha expirado.",
                            color=discord.Color.green()
                        )
                        await user.send(embed=embed)
                    except:
                        pass
                    
                    # Log al canal de moderación
                    log_embed = discord.Embed(
                        title="🔓 Mute Automático Expirado",
                        description=f"{user.mention} ha sido desmuteado automáticamente",
                        color=discord.Color.green(),
                        timestamp=datetime.now(timezone.utc)
                    )
                    log_embed.add_field(name="Usuario", value=f"{user.name} (`{user.id}`)")
                    await send_log(guild, log_embed)
                    
            except discord.Forbidden:
                logger.warning(f"Sin permisos para desmutear en {guild_id}")
            except Exception as e:
                logger.error(f"Error al desmutear: {e}")
    except Exception as e:
        logger.error(f"Error en check_mutes: {e}")

@tasks.loop(minutes=1)
async def check_lockdowns():
    """Revisa cada minuto si ya pasó el tiempo de lockdown"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT channel_id, guild_id, lockdown_start, lockdown_duration
                FROM lockdowns
                WHERE lockdown_start IS NOT NULL AND lockdown_duration IS NOT NULL
            """)
            rows = cursor.fetchall()

        for channel_id, guild_id, lockdown_start, lockdown_duration in rows:
            try:
                guild = client.get_guild(guild_id)
                if not guild:
                    continue

                channel = guild.get_channel(channel_id)
                if not channel:
                    continue

                lockdown_start_dt = datetime.fromisoformat(lockdown_start)
                current_time = datetime.now(timezone.utc)
                
                if current_time > lockdown_start_dt + timedelta(seconds=lockdown_duration):
                    default_role = guild.default_role
                    
                    await channel.set_permissions(
                        default_role,
                        send_messages=None,
                        add_reactions=None,
                        create_public_threads=None,
                        send_messages_in_threads=None,
                        reason="Lockdown expirado automáticamente"
                    )
                    
                    embed = discord.Embed(
                        title="🔓 Canal Desbloqueado",
                        description="El lockdown temporal ha expirado. El canal ha sido desbloqueado automáticamente.",
                        color=discord.Color.green(),
                        timestamp=datetime.now(timezone.utc)
                    )
                    await channel.send(embed=embed)
                    
                    clear_lockdown(channel_id, guild_id)
                    logger.info(f"✅ Canal #{channel.name} desbloqueado automáticamente en {guild.name}")
                    
            except discord.Forbidden:
                logger.warning(f"Sin permisos para desbloquear canal en {guild_id}")
            except Exception as e:
                logger.error(f"Error al desbloquear canal: {e}")
    except Exception as e:
        logger.error(f"Error en check_lockdowns: {e}")

@check_mutes.before_loop
async def before_check_mutes():
    await client.wait_until_ready()

@check_lockdowns.before_loop
async def before_check_lockdowns():
    await client.wait_until_ready()

# ------------------Comandos------------------#
        
# ------------------Comando de Ayuda------------------#

@tree.command(name="help", description="Muestra todos los comandos disponibles")
async def help_command(interaction: discord.Interaction):
    """Comando de ayuda mejorado con categorías"""
    
    embed = discord.Embed(
        title="📖 Guía de Comandos",
        description="Lista completa de comandos disponibles",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc)
    )
    
    # Comandos de moderación
    moderation = """
    `/warn <usuario> <razón>` - Advierte a un usuario
    `/clearwarns <usuario>` - Limpia los warns de un usuario
    `/checkwarns [usuario]` - Consulta warns
    `/mute <usuario> <duración> <razón>` - Mutea a un usuario
    `/unmute <usuario> <razón>` - Desmutea a un usuario
    `/muteinfo <usuario>` - Info sobre el mute
    `/kick <usuario> <razón>` - Expulsa a un usuario
    `/ban <usuario> <razón> [días]` - Banea a un usuario
    `/unban <user_id> <razón>` - Desbanea a un usuario
    """
    embed.add_field(name="👮 Moderación", value=moderation, inline=False)
    
    # Comandos de gestión de canales
    channels = """
    `/lockdown [canal] <duración> <razón>` - Bloquea un canal
    `/unlock [canal] <razón>` - Desbloquea un canal
    `/clear <cantidad> [usuario]` - Elimina mensajes
    `/slowmode <segundos>` - Establece modo lento
    """
    embed.add_field(name="📁 Canales", value=channels, inline=False)
    
    # Comandos de roles
    roles = """
    `/addrole <usuario> <rol> <razón>` - Asigna un rol
    `/removerole <usuario> <rol> <razón>` - Quita un rol
    `/roleinfo <rol>` - Info sobre un rol
    """
    embed.add_field(name="🎨 Roles", value=roles, inline=False)
    
    # Comandos de información
    info = """
    `/userinfo [usuario]` - Info de un usuario
    `/serverinfo` - Info del servidor
    `/ping` - Latencia del bot
    `/modlogs <usuario>` - Historial de moderación
    """
    embed.add_field(name="ℹ️ Información", value=info, inline=False)
    
    # Comandos de configuración
    config = """
    `/config view` - Ver configuración actual
    `/config set <opción> <valor>` - Configurar servidor
    """
    embed.add_field(name="⚙️ Configuración (Admin)", value=config, inline=False)
    
    # Formatos de tiempo
    embed.add_field(
        name="⏱️ Formatos de tiempo",
        value="`30s` (segundos), `5m` (minutos), `2h` (horas), `1d` (días), `1w` (semanas), `permanent`",
        inline=False
    )
    
    embed.set_footer(text="Usa los comandos con responsabilidad")
    embed.set_thumbnail(url=client.user.display_avatar.url)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)



#--------Comando para IA---------#
@tree.command(
    name="chat",
    description="Habla con la IA integrada del bot"
)
@app_commands.describe(mensaje="Escribe lo que quieras decirle a la IA")
async def chat(interaction: discord.Interaction, mensaje: str):
    await interaction.response.defer()  # Permite tiempo para procesar la respuesta

    try:
        # Crear modelo
        model = genai.GenerativeModel("gemini-1.5-flash")

        # Construir prompt
        full_prompt = f"{SYSTEM_PROMPT}\n\nUsuario: {mensaje}\nIA:"

        # Generar respuesta
        response = model.generate_content(full_prompt)

        # Enviar la respuesta al canal
        if response.text and response.text.strip():
            await interaction.followup.send(response.text[:2000])
        else:
            await interaction.followup.send("🤔 No obtuve respuesta de la IA, intenta reformular tu mensaje.")

    except Exception as e:
        await interaction.followup.send(f"❌ Error al contactar con la IA: {e}")


#--------demas comandos---------#

#ping pong pin
@tree.command(name="ping", description="Responde con Pong y muestra la latencia")
async def ping(interaction: discord.Interaction):
    """Comando ping mejorado con más información"""
    latency = round(client.latency * 1000)
    
    # Determinar color según latencia
    if latency < 100:
        color = discord.Color.green()
        status = "Excelente"
    elif latency < 200:
        color = discord.Color.yellow()
        status = "Buena"
    else:
        color = discord.Color.red()
        status = "Alta"
    
    embed = discord.Embed(
        title="🏓 Pong!",
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Latencia API", value=f"`{latency}ms`", inline=True)
    embed.add_field(name="Estado", value=status, inline=True)
    embed.set_footer(text=f"Solicitado por {interaction.user.name}")
    
    await interaction.response.send_message(embed=embed)

# Comando slash /saludo

# Lista de saludos bonitos
saludos = [
    "¡Hola {usuario}! 🌸 Que tu día esté lleno de sonrisas 😄✨",
    "¡Hey {usuario}! 🌟 Espero que tengas un día maravilloso 😎",
    "¡Saludos, {usuario}! 🌈 Que la alegría te acompañe hoy y siempre 😺",
    "¡Hola {usuario}! 💖 Disfruta de cada momento de este día tan especial 🌸",
    "¡Qué gusto verte, {usuario}! 🌟 ¡Que hoy sea increíble! 😄"
]

# Comando slash /saludo con saludos aleatorios
@tree.command(name="saludo", description="Saluda a un usuario de manera divertida")
@app_commands.describe(usuario="El usuario al que quieres saludar")
async def saludo(interaction: discord.Interaction, usuario: discord.Member):
    mensaje = random.choice(saludos).format(usuario=usuario.mention)

    embed = discord.Embed(
        title="🌸 ¡Saludo especial! 🌸",
        description=mensaje,
        color=discord.Color.purple()
    )
    embed.set_thumbnail(url=usuario.display_avatar.url)
    embed.set_footer(text=f"Saludado por {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)

    await interaction.response.send_message(embed=embed)

#comando para dados
@tree.command(name="tirardado", description="Lanza un dado de 6 caras")
async def tirardado(interaction: discord.Interaction):
    import random
    resultado = random.randint(1, 6)
    await interaction.response.send_message(f"🎲 {interaction.user.mention} tiró el dado y salió **{resultado}**"

#comando para cara o cruz
tree.command(name="moneda", description="Lanza una moneda al aire")
async def moneda(interaction: discord.Interaction):
    import random
    resultado = random.choice(["Cara 🪙", "Cruz 🪙"])
    await interaction.response.send_message(f"{interaction.user.mention} lanzó la moneda y salió **{resultado}**")



#encuesta
@tree.command(name="encuesta", description="Crea una encuesta sí/no")
@app_commands.describe(pregunta="Pregunta para la encuesta")
async def encuesta(interaction: discord.Interaction, pregunta: str):
    class EncuestaView(View):
        def __init__(self):
            super().__init__()
            self.resultado = {"Sí": 0, "No": 0}

        @discord.ui.button(label="Sí", style=discord.ButtonStyle.green)
        async def si(self, button: Button, inter: discord.Interaction):
            self.resultado["Sí"] += 1
            await inter.response.edit_message(content=f"✅ Sí: {self.resultado['Sí']} | ❌ No: {self.resultado['No']}", view=self)

        @discord.ui.button(label="No", style=discord.ButtonStyle.red)
        async def no(self, button: Button, inter: discord.Interaction):
            self.resultado["No"] += 1
            await inter.response.edit_message(content=f"✅ Sí: {self.resultado['Sí']} | ❌ No: {self.resultado['No']}", view=self)

    await interaction.response.send_message(f"📊 **Encuesta:** {pregunta}", view=EncuestaView())



# ------------------Comandos de Moderación------------------#

@tree.command(name="warn", description="Advierte a un usuario (configurable en /config)")
@app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
@app_commands.describe(
    user="El usuario a advertir",
    reason="Motivo de la advertencia"
)
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str = "No especificado"):
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("❌ No tienes permisos para usar este comando.", ephemeral=True)
        return
    
    if interaction.user.id == user.id:
        await interaction.response.send_message("❌ No puedes advertirte a ti mismo.", ephemeral=True)
        return
    
    if user.bot:
        await interaction.response.send_message("❌ No puedes advertir a un bot.", ephemeral=True)
        return
    
    if not has_higher_role(interaction.user, user) and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("❌ No puedes advertir a alguien con un rol igual o superior.", ephemeral=True)
        return

    config = get_guild_config(interaction.guild.id)
    max_warns = config['max_warns']
    warns = get_warns(user.id, interaction.guild.id)
    
    # Log de acción
    log_action(user.id, interaction.guild.id, "WARN", interaction.user.id, reason)

    embed = discord.Embed(
        title="⚠️ Usuario Advertido",
        description=f"{user.mention} ha sido advertido",
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="👤 Usuario", value=user.mention, inline=True)
    embed.add_field(name="👮 Moderador", value=interaction.user.mention, inline=True)
    embed.add_field(name="📋 Warns", value=f"`{warns}/{max_warns}`", inline=True)
    embed.add_field(name="📝 Razón", value=reason, inline=False)
    embed.set_footer(text=f"ID: {user.id}")
    
    await interaction.response.send_message(embed=embed)
    
    # Notificar al usuario
    try:
        dm_embed = discord.Embed(
            title="⚠️ Has recibido una advertencia",
            description=f"Has sido advertido en **{interaction.guild.name}**",
            color=discord.Color.orange()
        )
        dm_embed.add_field(name="👮 Moderador", value=interaction.user.name, inline=True)
        dm_embed.add_field(name="📋 Warns", value=f"{warns}/{max_warns}", inline=True)
        dm_embed.add_field(name="📝 Razón", value=reason, inline=False)
        await user.send(embed=dm_embed)
    except:
        pass
    
    # Log al canal
    await send_log(interaction.guild, embed)

    # Auto-mute al alcanzar máximo de warns
    if warns >= max_warns:
        muted_role = await get_or_create_muted_role(interaction.guild)
        
        if not muted_role:
            await interaction.followup.send("⚠️ No se pudo crear el rol de silenciado.", ephemeral=True)
            return

        if muted_role in user.roles:
            await interaction.followup.send(f"⚠️ {user.mention} ya está silenciado.")
            return

        try:
            await user.add_roles(muted_role, reason=f"Mute automático por {max_warns} warns")
            auto_mute_duration = config['auto_mute_duration']
            
            set_mute(user.id, interaction.guild.id, auto_mute_duration)
            reset_warns(user.id, interaction.guild.id)
            
            mute_embed = discord.Embed(
                title="🚫 Mute Automático",
                description=f"{user.mention} ha sido silenciado por alcanzar {max_warns} warns",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
            mute_embed.add_field(name="⏱️ Duración", value=format_duration(auto_mute_duration))
            
            await interaction.followup.send(embed=mute_embed)
            await send_log(interaction.guild, mute_embed)
            
        except discord.Forbidden:
            await interaction.followup.send("❌ No tengo permisos para silenciar a este usuario.", ephemeral=True)

@tree.command(name="checkwarns", description="Consulta los warns de un usuario")
@app_commands.describe(user="El usuario a consultar (opcional, por defecto tú mismo)")
async def checkwarns(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    warns = get_user_warns_count(target.id, interaction.guild.id)
    config = get_guild_config(interaction.guild.id)
    max_warns = config['max_warns']
    
    embed = discord.Embed(
        title="📊 Consulta de Warns",
        color=discord.Color.blue()
    )
    embed.add_field(name="👤 Usuario", value=target.mention, inline=True)
    embed.add_field(name="⚠️ Warns", value=f"`{warns}/{max_warns}`", inline=True)
    
    if warns > 0:
        porcentaje = (warns / max_warns) * 100
        if porcentaje >= 80:
            embed.color = discord.Color.red()
            embed.add_field(name="Estado", value="🔴 Crítico", inline=True)
        elif porcentaje >= 50:
            embed.color = discord.Color.orange()
            embed.add_field(name="Estado", value="🟡 Advertencia", inline=True)
        else:
            embed.color = discord.Color.green()
            embed.add_field(name="Estado", value="🟢 Normal", inline=True)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="clearwarns", description="Limpia los warns de un usuario")
@app_commands.describe(user="El usuario al que resetear los warns", reason="Razón del reseteo")
async def clearwarns(interaction: discord.Interaction, user: discord.Member, reason: str = "No especificado"):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Solo administradores pueden usar este comando.", ephemeral=True)
        return
    
    old_warns = get_user_warns_count(user.id, interaction.guild.id)
    
    if old_warns == 0:
        await interaction.response.send_message(f"ℹ️ {user.mention} no tiene warns.", ephemeral=True)
        return
    
    if reset_warns(user.id, interaction.guild.id):
        log_action(user.id, interaction.guild.id, "CLEAR_WARNS", interaction.user.id, reason)
        
        embed = discord.Embed(
            title="✅ Warns Reseteados",
            description=f"Los warns de {user.mention} han sido limpiados",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="👤 Usuario", value=user.mention, inline=True)
        embed.add_field(name="👮 Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="📋 Warns removidos", value=f"`{old_warns}`", inline=True)
        embed.add_field(name="📝 Razón", value=reason, inline=False)
        
        await interaction.response.send_message(embed=embed)
        await send_log(interaction.guild, embed)
    else:
        await interaction.response.send_message("❌ Error al resetear warns.", ephemeral=True)

@tree.command(name="mute", description="Mutea a un usuario por un tiempo específico o permanente")
@app_commands.describe(
    user="El usuario a mutear",
    duration="Duración del mute (ej: 30m, 2h, 1d) o 'permanent'",
    reason="Razón del mute"
)
async def mute(interaction: discord.Interaction, user: discord.Member, duration: str, reason: str = "No especificado"):
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("❌ No tienes permisos para usar este comando.", ephemeral=True)
        return
    
    if interaction.user.id == user.id:
        await interaction.response.send_message("❌ No puedes mutearte a ti mismo.", ephemeral=True)
        return
    
    if user.bot:
        await interaction.response.send_message("❌ No puedes mutear a un bot.", ephemeral=True)
        return
    
    if not has_higher_role(interaction.user, user) and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("❌ No puedes mutear a alguien con un rol igual o superior.", ephemeral=True)
        return
    
    muted_role = await get_or_create_muted_role(interaction.guild)
    
    if not muted_role:
        await interaction.response.send_message("❌ No se pudo crear el rol 'Silenciado'.", ephemeral=True)
        return
    
    if muted_role in user.roles:
        await interaction.response.send_message(f"⚠️ {user.mention} ya está silenciado. Usa `/muteinfo` para más detalles.", ephemeral=True)
        return
    
    is_permanent = duration.lower() in ['permanent', 'permanente', 'perm', 'p']
    duration_seconds = None
    
    if not is_permanent:
        duration_seconds = parse_time(duration)
        if duration_seconds is None:
            await interaction.response.send_message(
                "❌ Formato de duración inválido.\n**Ejemplos válidos:** `30s`, `5m`, `2h`, `1d`, `1w`, `permanent`",
                ephemeral=True
            )
            return
        
        if duration_seconds > 2592000:
            await interaction.response.send_message("❌ La duración máxima es de 30 días. Para mutes más largos usa `permanent`.", ephemeral=True)
            return
    
    try:
        await user.add_roles(muted_role, reason=f"Mute: {reason} | Moderador: {interaction.user.name}")
        set_mute(user.id, interaction.guild.id, duration_seconds)
        log_action(user.id, interaction.guild.id, "MUTE", interaction.user.id, reason)
        
        embed = discord.Embed(
            title="🔇 Usuario Muteado",
            description=f"{user.mention} ha sido silenciado",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="👤 Usuario", value=user.mention, inline=True)
        embed.add_field(name="👮 Moderador", value=interaction.user.mention, inline=True)
        
        if is_permanent:
            embed.add_field(name="⏱️ Duración", value="**♾️ PERMANENTE**", inline=True)
        else:
            embed.add_field(name="⏱️ Duración", value=format_duration(duration_seconds), inline=True)
            mute_end = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
            embed.add_field(name="🕐 Expira", value=f"<t:{int(mute_end.timestamp())}:R>", inline=True)
        
        embed.add_field(name="📝 Razón", value=reason, inline=False)
        embed.set_footer(text=f"ID: {user.id}")
        
        await interaction.response.send_message(embed=embed)
        await send_log(interaction.guild, embed)
        
        # Notificar al usuario
        try:
            dm_embed = discord.Embed(
                title="🔇 Has sido muteado",
                description=f"Has sido silenciado en **{interaction.guild.name}**",
                color=discord.Color.red()
            )
            dm_embed.add_field(name="👮 Moderador", value=interaction.user.name, inline=True)
            dm_embed.add_field(name="⏱️ Duración", value="Permanente" if is_permanent else format_duration(duration_seconds), inline=True)
            dm_embed.add_field(name="📝 Razón", value=reason, inline=False)
            await user.send(embed=dm_embed)
        except:
            pass
            
    except discord.Forbidden:
        await interaction.response.send_message("❌ No tengo permisos para mutear a este usuario.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error en mute: {e}")
        await interaction.response.send_message(f"❌ Error al mutear: {str(e)}", ephemeral=True)

@tree.command(name="unmute", description="Desmutea a un usuario antes de tiempo")
@app_commands.describe(user="El usuario a desmutear", reason="Razón del desmuteo anticipado")
async def unmute(interaction: discord.Interaction, user: discord.Member, reason: str = "No especificado"):
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("❌ No tienes permisos para usar este comando.", ephemeral=True)
        return
    
    muted_role = discord.utils.get(interaction.guild.roles, name="Silenciado")
    
    if not muted_role or muted_role not in user.roles:
        await interaction.response.send_message(f"⚠️ {user.mention} no está silenciado.", ephemeral=True)
        return
    
    mute_info = get_mute_info(user.id, interaction.guild.id)
    time_remaining = "Desconocido"
    
    if mute_info and mute_info['mute_duration']:
        mute_start = datetime.fromisoformat(mute_info['mute_start'])
        mute_end = mute_start + timedelta(seconds=mute_info['mute_duration'])
        time_left = mute_end - datetime.now(timezone.utc)
        
        if time_left.total_seconds() > 0:
            time_remaining = format_duration(int(time_left.total_seconds()))
    
    try:
        await user.remove_roles(muted_role, reason=f"Unmute: {reason} | Moderador: {interaction.user.name}")
        clear_mute_data(user.id, interaction.guild.id)
        log_action(user.id, interaction.guild.id, "UNMUTE", interaction.user.id, reason)
        
        embed = discord.Embed(
            title="🔓 Usuario Desmuteado",
            description=f"{user.mention} ha sido desmuteado anticipadamente",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="👤 Usuario", value=user.mention, inline=True)
        embed.add_field(name="👮 Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="⏱️ Tiempo restante", value=time_remaining, inline=True)
        embed.add_field(name="📝 Razón", value=reason, inline=False)
        embed.set_footer(text=f"ID: {user.id}")
        
        await interaction.response.send_message(embed=embed)
        await send_log(interaction.guild, embed)
        
        # Notificar al usuario
        try:
            dm_embed = discord.Embed(
                title="🔓 Has sido desmuteado",
                description=f"Has sido desmuteado en **{interaction.guild.name}**",
                color=discord.Color.green()
            )
            dm_embed.add_field(name="👮 Moderador", value=interaction.user.name, inline=True)
            dm_embed.add_field(name="📝 Razón", value=reason, inline=False)
            await user.send(embed=dm_embed)
        except:
            pass
            
    except discord.Forbidden:
        await interaction.response.send_message("❌ No tengo permisos para quitar el rol a este usuario.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error en unmute: {e}")
        await interaction.response.send_message(f"❌ Error al desmutear: {str(e)}", ephemeral=True)

@tree.command(name="muteinfo", description="Muestra información sobre el mute de un usuario")
@app_commands.describe(user="El usuario a consultar")
async def muteinfo(interaction: discord.Interaction, user: discord.Member):
    muted_role = discord.utils.get(interaction.guild.roles, name="Silenciado")
    
    if not muted_role or muted_role not in user.roles:
        await interaction.response.send_message(f"ℹ️ {user.mention} no está actualmente silenciado.", ephemeral=True)
        return
    
    mute_info = get_mute_info(user.id, interaction.guild.id)
    
    if not mute_info:
        await interaction.response.send_message(f"⚠️ {user.mention} tiene el rol de silenciado pero no hay registro en la base de datos.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title="🔇 Información de Mute",
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="👤 Usuario", value=user.mention, inline=True)
    embed.add_field(name="🆔 ID", value=user.id, inline=True)
    
    if mute_info['mute_duration'] is None:
        embed.add_field(name="⏱️ Duración", value="**♾️ PERMANENTE**", inline=True)
        embed.add_field(name="🕐 Inicio del mute", value=f"<t:{int(datetime.fromisoformat(mute_info['mute_start']).timestamp())}:R>", inline=False)
        embed.set_footer(text="Este mute no expirará automáticamente")
    else:
        mute_start = datetime.fromisoformat(mute_info['mute_start'])
        mute_end = mute_start + timedelta(seconds=mute_info['mute_duration'])
        time_left = mute_end - datetime.now(timezone.utc)
        
        time_remaining = format_duration(int(time_left.total_seconds())) if time_left.total_seconds() > 0 else "El mute debería haber expirado"
        
        embed.add_field(name="⏱️ Tiempo restante", value=time_remaining, inline=True)
        embed.add_field(name="🕐 Inicio", value=f"<t:{int(mute_start.timestamp())}:R>", inline=True)
        embed.add_field(name="🕐 Fin", value=f"<t:{int(mute_end.timestamp())}:R>", inline=True)
        embed.add_field(name="⏳ Duración total", value=format_duration(mute_info['mute_duration']), inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="kick", description="Expulsa a un usuario del servidor")
@app_commands.describe(user="El usuario a expulsar", reason="Razón de la expulsión")
async def kick(interaction: discord.Interaction, user: discord.Member, reason: str = "No especificado"):
    if not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message("❌ No tienes permisos para expulsar miembros.", ephemeral=True)
        return
    
    if interaction.user.id == user.id:
        await interaction.response.send_message("❌ No puedes expulsarte a ti mismo.", ephemeral=True)
        return
    
    if user.bot:
        await interaction.response.send_message("❌ No puedes expulsar a un bot.", ephemeral=True)
        return
    
    if user.id == interaction.guild.owner_id:
        await interaction.response.send_message("❌ No puedes expulsar al dueño del servidor.", ephemeral=True)
        return
    
    if not has_higher_role(interaction.user, user) and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("❌ No puedes expulsar a alguien con un rol igual o superior al tuyo.", ephemeral=True)
        return
    
    bot_member = interaction.guild.get_member(client.user.id)
    if not has_higher_role(bot_member, user):
        await interaction.response.send_message("❌ No puedo expulsar a este usuario porque tiene un rol igual o superior al mío.", ephemeral=True)
        return
    
    try:
        # Notificar al usuario
        try:
            dm_embed = discord.Embed(
                title="👢 Has sido expulsado",
                description=f"Has sido expulsado de **{interaction.guild.name}**",
                color=discord.Color.orange()
            )
            dm_embed.add_field(name="👮 Moderador", value=interaction.user.name, inline=True)
            dm_embed.add_field(name="📝 Razón", value=reason, inline=False)
            dm_embed.set_footer(text="Puedes volver a unirte al servidor si tienes una invitación")
            await user.send(embed=dm_embed)
        except:
            pass
        
        await user.kick(reason=f"{reason} | Moderador: {interaction.user.name}")
        log_action(user.id, interaction.guild.id, "KICK", interaction.user.id, reason)
        
        embed = discord.Embed(
            title="👢 Usuario Expulsado",
            description=f"**{user.name}** ha sido expulsado del servidor",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="👤 Usuario", value=f"{user.name}\n`{user.id}`", inline=True)
        embed.add_field(name="👮 Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="📝 Razón", value=reason, inline=False)
        embed.set_footer(text=f"ID: {user.id}")
        
        await interaction.response.send_message(embed=embed)
        await send_log(interaction.guild, embed)
        
    except discord.Forbidden:
        await interaction.response.send_message("❌ No tengo permisos para expulsar a este usuario.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error en kick: {e}")
        await interaction.response.send_message(f"❌ Error al expulsar: {str(e)}", ephemeral=True)

@tree.command(name="ban", description="Banea a un usuario del servidor")
@app_commands.describe(
    user="El usuario a banear",
    reason="Razón del baneo",
    delete_messages="Días de mensajes a eliminar (0-7)"
)
async def ban(interaction: discord.Interaction, user: discord.Member, reason: str = "No especificado", delete_messages: int = 0):
    if not interaction.user.guild_permissions.ban_members:
        await interaction.response.send_message("❌ No tienes permisos para banear miembros.", ephemeral=True)
        return
    
    if interaction.user.id == user.id:
        await interaction.response.send_message("❌ No puedes banearte a ti mismo.", ephemeral=True)
        return
    
    if user.bot:
        await interaction.response.send_message("❌ No puedes banear a un bot.", ephemeral=True)
        return
    
    if user.id == interaction.guild.owner_id:
        await interaction.response.send_message("❌ No puedes banear al dueño del servidor.", ephemeral=True)
        return
    
    if not has_higher_role(interaction.user, user) and interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("❌ No puedes banear a alguien con un rol igual o superior al tuyo.", ephemeral=True)
        return
    
    bot_member = interaction.guild.get_member(client.user.id)
    if not has_higher_role(bot_member, user):
        await interaction.response.send_message("❌ No puedo banear a este usuario porque tiene un rol igual o superior al mío.", ephemeral=True)
        return
    
    if delete_messages < 0 or delete_messages > 7:
        await interaction.response.send_message("❌ Los días de mensajes a eliminar deben estar entre 0 y 7.", ephemeral=True)
        return
    
    try:
        # Notificar al usuario
        try:
            dm_embed = discord.Embed(
                title="🔨 Has sido baneado",
                description=f"Has sido baneado permanentemente de **{interaction.guild.name}**",
                color=discord.Color.dark_red()
            )
            dm_embed.add_field(name="👮 Moderador", value=interaction.user.name, inline=True)
            dm_embed.add_field(name="📝 Razón", value=reason, inline=False)
            dm_embed.set_footer(text="Este baneo es permanente")
            await user.send(embed=dm_embed)
        except:
            pass
        
        await user.ban(
            reason=f"{reason} | Moderador: {interaction.user.name}",
            delete_message_days=delete_messages
        )
        log_action(user.id, interaction.guild.id, "BAN", interaction.user.id, reason)
        
        embed = discord.Embed(
            title="🔨 Usuario Baneado",
            description=f"**{user.name}** ha sido baneado permanentemente",
            color=discord.Color.dark_red(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="👤 Usuario", value=f"{user.name}\n`{user.id}`", inline=True)
        embed.add_field(name="👮 Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="🗑️ Mensajes eliminados", value=f"{delete_messages} días", inline=True)
        embed.add_field(name="📝 Razón", value=reason, inline=False)
        embed.set_footer(text=f"ID: {user.id}")
        
        await interaction.response.send_message(embed=embed)
        await send_log(interaction.guild, embed)
        
    except discord.Forbidden:
        await interaction.response.send_message("❌ No tengo permisos para banear a este usuario.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error en ban: {e}")
        await interaction.response.send_message(f"❌ Error al banear: {str(e)}", ephemeral=True)

@tree.command(name="unban", description="Desbanea a un usuario del servidor")
@app_commands.describe(user_id="ID del usuario a desbanear", reason="Razón del desbaneo")
async def unban(interaction: discord.Interaction, user_id: str, reason: str = "No especificado"):
    if not interaction.user.guild_permissions.ban_members:
        await interaction.response.send_message("❌ No tienes permisos para desbanear miembros.", ephemeral=True)
        return
    
    try:
        user_id_int = int(user_id)
    except ValueError:
        await interaction.response.send_message("❌ El ID proporcionado no es válido.", ephemeral=True)
        return
    
    try:
        await interaction.response.defer()
        
        bans = [ban async for ban in interaction.guild.bans()]
        
        user_to_unban = None
        for ban_entry in bans:
            if ban_entry.user.id == user_id_int:
                user_to_unban = ban_entry.user
                break
        
        if not user_to_unban:
            await interaction.followup.send(f"❌ No se encontró ningún usuario baneado con ID `{user_id}`")
            return
        
        await interaction.guild.unban(user_to_unban, reason=f"{reason} | Moderador: {interaction.user.name}")
        log_action(user_id_int, interaction.guild.id, "UNBAN", interaction.user.id, reason)
        
        embed = discord.Embed(
            title="✅ Usuario Desbaneado",
            description=f"**{user_to_unban.name}** ha sido desbaneado",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_thumbnail(url=user_to_unban.display_avatar.url)
        embed.add_field(name="👤 Usuario", value=f"{user_to_unban.name}\n`{user_to_unban.id}`", inline=True)
        embed.add_field(name="👮 Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="📝 Razón", value=reason, inline=False)
        embed.set_footer(text=f"ID: {user_to_unban.id}")
        
        await interaction.followup.send(embed=embed)
        await send_log(interaction.guild, embed)
        
    except discord.Forbidden:
        await interaction.followup.send("❌ No tengo permisos para desbanear usuarios.")
    except Exception as e:
        logger.error(f"Error en unban: {e}")
        await interaction.followup.send(f"❌ Error al desbanear: {str(e)}")

# ------------------Comandos de Gestión de Canales------------------#

@tree.command(name="lockdown", description="Bloquea un canal para que nadie pueda enviar mensajes")
@app_commands.describe(
    channel="El canal a bloquear (opcional, por defecto el canal actual)",
    duration="Duración del lockdown (ej: 30m, 2h, 1d) o 'permanent'",
    reason="Razón del lockdown"
)
async def lockdown(interaction: discord.Interaction, channel: discord.TextChannel = None, duration: str = "permanent", reason: str = "No especificado"):
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("❌ No tienes permisos para gestionar canales.", ephemeral=True)
        return
    
    target_channel = channel or interaction.channel
    
    if not isinstance(target_channel, discord.TextChannel):
        await interaction.response.send_message("❌ Solo puedes bloquear canales de texto.", ephemeral=True)
        return
    
    if get_lockdown_info(target_channel.id, interaction.guild.id):
        await interaction.response.send_message(f"⚠️ {target_channel.mention} ya está en lockdown. Usa `/unlock` para desbloquearlo primero.", ephemeral=True)
        return
    
    is_permanent = duration.lower() in ['permanent', 'permanente', 'perm', 'p']
    duration_seconds = None
    
    if not is_permanent:
        duration_seconds = parse_time(duration)
        if duration_seconds is None:
            await interaction.response.send_message(
                "❌ Formato de duración inválido.\n**Ejemplos válidos:** `30m`, `2h`, `6h`, `1d`, `permanent`",
                ephemeral=True
            )
            return
        
        if duration_seconds > 604800:
            await interaction.response.send_message("❌ La duración máxima es de 7 días. Para lockdowns más largos usa `permanent`.", ephemeral=True)
            return
    
    try:
        default_role = interaction.guild.default_role
        
        await target_channel.set_permissions(
            default_role,
            send_messages=False,
            add_reactions=False,
            create_public_threads=False,
            send_messages_in_threads=False,
            reason=f"Lockdown: {reason} | Moderador: {interaction.user.name}"
        )
        
        set_lockdown(target_channel.id, interaction.guild.id, duration_seconds)
        log_action(0, interaction.guild.id, "LOCKDOWN", interaction.user.id, f"Canal: {target_channel.name} - {reason}")
        
        embed = discord.Embed(
            title="🔒 Canal Bloqueado",
            description=f"{target_channel.mention} ha sido bloqueado",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="📁 Canal", value=target_channel.mention, inline=True)
        embed.add_field(name="👮 Moderador", value=interaction.user.mention, inline=True)
        
        if is_permanent:
            embed.add_field(name="⏱️ Duración", value="**♾️ PERMANENTE**", inline=True)
        else:
            embed.add_field(name="⏱️ Duración", value=format_duration(duration_seconds), inline=True)
            lockdown_end = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
            embed.add_field(name="🕐 Se desbloqueará", value=f"<t:{int(lockdown_end.timestamp())}:R>", inline=True)
        
        embed.add_field(name="📝 Razón", value=reason, inline=False)
        embed.set_footer(text=f"Canal ID: {target_channel.id}")
        
        await interaction.response.send_message(embed=embed)
        await send_log(interaction.guild, embed)
        
        lockdown_msg = discord.Embed(
            title="🔒 Canal Bloqueado",
            description="Este canal ha sido bloqueado por el equipo de moderación.",
            color=discord.Color.red()
        )
        lockdown_msg.add_field(name="👮 Moderador", value=interaction.user.mention, inline=True)
        lockdown_msg.add_field(name="⏱️ Duración", value="Permanente" if is_permanent else format_duration(duration_seconds), inline=True)
        lockdown_msg.add_field(name="📝 Razón", value=reason, inline=False)
        
        await target_channel.send(embed=lockdown_msg)
        
    except discord.Forbidden:
        await interaction.response.send_message("❌ No tengo permisos para bloquear este canal.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error en lockdown: {e}")
        await interaction.response.send_message(f"❌ Error al bloquear el canal: {str(e)}", ephemeral=True)

@tree.command(name="unlock", description="Desbloquea un canal previamente bloqueado")
@app_commands.describe(
    channel="El canal a desbloquear (opcional, por defecto el canal actual)",
    reason="Razón del desbloqueo"
)
async def unlock(interaction: discord.Interaction, channel: discord.TextChannel = None, reason: str = "No especificado"):
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("❌ No tienes permisos para gestionar canales.", ephemeral=True)
        return
    
    target_channel = channel or interaction.channel
    
    if not isinstance(target_channel, discord.TextChannel):
        await interaction.response.send_message("❌ Solo puedes desbloquear canales de texto.", ephemeral=True)
        return
    
    lockdown_info = get_lockdown_info(target_channel.id, interaction.guild.id)
    
    try:
        default_role = interaction.guild.default_role
        
        await target_channel.set_permissions(
            default_role,
            send_messages=None,
            add_reactions=None,
            create_public_threads=None,
            send_messages_in_threads=None,
            reason=f"Unlock: {reason} | Moderador: {interaction.user.name}"
        )
        
        time_locked = "Desconocido"
        if lockdown_info:
            lockdown_start = datetime.fromisoformat(lockdown_info['lockdown_start'])
            time_diff = datetime.now(timezone.utc) - lockdown_start
            time_locked = format_duration(int(time_diff.total_seconds()))
            clear_lockdown(target_channel.id, interaction.guild.id)
        
        log_action(0, interaction.guild.id, "UNLOCK", interaction.user.id, f"Canal: {target_channel.name} - {reason}")
        
        embed = discord.Embed(
            title="🔓 Canal Desbloqueado",
            description=f"{target_channel.mention} ha sido desbloqueado",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="📁 Canal", value=target_channel.mention, inline=True)
        embed.add_field(name="👮 Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="⏱️ Tiempo bloqueado", value=time_locked, inline=True)
        embed.add_field(name="📝 Razón", value=reason, inline=False)
        embed.set_footer(text=f"Canal ID: {target_channel.id}")
        
        await interaction.response.send_message(embed=embed)
        await send_log(interaction.guild, embed)
        
        unlock_msg = discord.Embed(
            title="🔓 Canal Desbloqueado",
            description="Este canal ha sido desbloqueado. Pueden volver a enviar mensajes.",
            color=discord.Color.green()
        )
        unlock_msg.add_field(name="👮 Moderador", value=interaction.user.mention, inline=True)
        unlock_msg.add_field(name="📝 Razón", value=reason, inline=False)
        
        await target_channel.send(embed=unlock_msg)
        
    except discord.Forbidden:
        await interaction.response.send_message("❌ No tengo permisos para desbloquear este canal.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error en unlock: {e}")
        await interaction.response.send_message(f"❌ Error al desbloquear el canal: {str(e)}", ephemeral=True)

@tree.command(name="clear", description="Elimina una cantidad específica de mensajes")
@app_commands.describe(
    amount="Cantidad de mensajes a eliminar (1-100)",
    user="Usuario específico cuyos mensajes eliminar (opcional)"
)
async def clear(interaction: discord.Interaction, amount: int, user: discord.Member = None):
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.response.send_message("❌ No tienes permisos para gestionar mensajes.", ephemeral=True)
        return
    
    if amount < 1 or amount > 100:
        await interaction.response.send_message("❌ La cantidad debe estar entre 1 y 100.", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        def check_message(m):
            if user:
                return m.author.id == user.id
            return True
        
        deleted = await interaction.channel.purge(limit=amount, check=check_message)
        
        log_action(user.id if user else 0, interaction.guild.id, "CLEAR", interaction.user.id, 
                   f"Canal: {interaction.channel.name} - Mensajes eliminados: {len(deleted)}")
        
        embed = discord.Embed(
            title="🗑️ Mensajes Eliminados",
            description=f"Se han eliminado **{len(deleted)}** mensajes",
            color=discord.Color.blue()
        )
        if user:
            embed.add_field(name="👤 Usuario", value=user.mention)
        embed.add_field(name="📁 Canal", value=interaction.channel.mention)
        embed.add_field(name="👮 Moderador", value=interaction.user.mention)
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        await send_log(interaction.guild, embed)
        
    except discord.Forbidden:
        await interaction.followup.send("❌ No tengo permisos para eliminar mensajes.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error en clear: {e}")
        await interaction.followup.send(f"❌ Error al eliminar mensajes: {str(e)}", ephemeral=True)

@tree.command(name="slowmode", description="Establece el modo lento en un canal")
@app_commands.describe(
    seconds="Segundos de delay entre mensajes (0 para desactivar, máx 21600)",
    channel="Canal a modificar (opcional, por defecto el actual)"
)
async def slowmode(interaction: discord.Interaction, seconds: int, channel: discord.TextChannel = None):
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("❌ No tienes permisos para gestionar canales.", ephemeral=True)
        return
    
    target_channel = channel or interaction.channel
    
    if seconds < 0 or seconds > 21600:
        await interaction.response.send_message("❌ Los segundos deben estar entre 0 y 21600 (6 horas).", ephemeral=True)
        return
    
    try:
        await target_channel.edit(slowmode_delay=seconds)
        
        if seconds == 0:
            embed = discord.Embed(
                title="⚡ Modo Lento Desactivado",
                description=f"El modo lento ha sido desactivado en {target_channel.mention}",
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="🐌 Modo Lento Activado",
                description=f"Modo lento establecido en {target_channel.mention}",
                color=discord.Color.orange()
            )
            embed.add_field(name="⏱️ Delay", value=f"{seconds} segundos")
        
        embed.add_field(name="👮 Moderador", value=interaction.user.mention)
        
        await interaction.response.send_message(embed=embed)
        await send_log(interaction.guild, embed)
        
    except discord.Forbidden:
        await interaction.response.send_message("❌ No tengo permisos para modificar este canal.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error en slowmode: {e}")
        await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

# ------------------Comandos de Roles------------------#

@tree.command(name="addrole", description="Asigna un rol a un usuario")
@app_commands.describe(
    user="El usuario al que asignar el rol",
    role="El rol a asignar",
    reason="Razón de la asignación"
)
async def addrole(interaction: discord.Interaction, user: discord.Member, role: discord.Role, reason: str = "No especificado"):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("❌ No tienes permisos para gestionar roles.", ephemeral=True)
        return
    
    if role.is_default():
        await interaction.response.send_message("❌ No puedes asignar el rol @everyone.", ephemeral=True)
        return
    
    # Evitar que un moderador asigne roles iguales o superiores a los suyos
    if interaction.user.id != interaction.guild.owner_id and role >= interaction.user.top_role:
        await interaction.response.send_message("❌ No puedes asignar un rol igual o superior al tuyo.", ephemeral=True)
        return
    
    bot_member = interaction.guild.get_member(client.user.id)
    if role >= bot_member.top_role:
        await interaction.response.send_message("❌ No puedo asignar un rol igual o superior al mío.", ephemeral=True)
        return
    
    if role in user.roles:
        await interaction.response.send_message(f"⚠️ {user.mention} ya tiene el rol {role.mention}.", ephemeral=True)
        return
    
    if role.managed:
        await interaction.response.send_message("❌ No puedes asignar este rol porque está gestionado por una integración o bot.", ephemeral=True)
        return
    
    try:
        await user.add_roles(role, reason=f"{reason} | Moderador: {interaction.user.name}")
        log_action(user.id, interaction.guild.id, "ADD_ROLE", interaction.user.id, f"Rol: {role.name} - {reason}")
        
        embed = discord.Embed(
            title="✅ Rol Asignado",
            description=f"Se ha asignado el rol {role.mention} a {user.mention}",
            color=role.color if role.color != discord.Color.default() else discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="👤 Usuario", value=user.mention, inline=True)
        embed.add_field(name="🎨 Rol", value=role.mention, inline=True)
        embed.add_field(name="👮 Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="📝 Razón", value=reason, inline=False)
        embed.set_footer(text=f"Usuario ID: {user.id} | Rol ID: {role.id}")
        
        await interaction.response.send_message(embed=embed)
        await send_log(interaction.guild, embed)
        
    except discord.Forbidden:
        await interaction.response.send_message("❌ No tengo permisos para asignar este rol.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error en addrole: {e}")
        await interaction.response.send_message(f"❌ Error al asignar el rol: {str(e)}", ephemeral=True)

@tree.command(name="removerole", description="Quita un rol de un usuario")
@app_commands.describe(
    user="El usuario al que quitar el rol",
    role="El rol a quitar",
    reason="Razón de la remoción"
)
async def removerole(interaction: discord.Interaction, user: discord.Member, role: discord.Role, reason: str = "No especificado"):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("❌ No tienes permisos para gestionar roles.", ephemeral=True)
        return
    
    if role.is_default():
        await interaction.response.send_message("❌ No puedes quitar el rol @everyone.", ephemeral=True)
        return
    
    # Evitar que un moderador quite roles iguales o superiores a los suyos
    if interaction.user.id != interaction.guild.owner_id and role >= interaction.user.top_role:
        await interaction.response.send_message("❌ No puedes quitar un rol igual o superior al tuyo.", ephemeral=True)
        return
    
    bot_member = interaction.guild.get_member(client.user.id)
    if role >= bot_member.top_role:
        await interaction.response.send_message("❌ No puedo quitar un rol igual o superior al mío.", ephemeral=True)
        return
    
    if role not in user.roles:
        await interaction.response.send_message(f"⚠️ {user.mention} no tiene el rol {role.mention}.", ephemeral=True)
        return
    
    if role.managed:
        await interaction.response.send_message("❌ No puedes quitar este rol porque está gestionado por una integración o bot.", ephemeral=True)
        return
    
    try:
        await user.remove_roles(role, reason=f"{reason} | Moderador: {interaction.user.name}")
        log_action(user.id, interaction.guild.id, "REMOVE_ROLE", interaction.user.id, f"Rol: {role.name} - {reason}")
        
        embed = discord.Embed(
            title="🗑️ Rol Removido",
            description=f"Se ha quitado el rol {role.mention} de {user.mention}",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="👤 Usuario", value=user.mention, inline=True)
        embed.add_field(name="🎨 Rol", value=role.mention, inline=True)
        embed.add_field(name="👮 Moderador", value=interaction.user.mention, inline=True)
        embed.add_field(name="📝 Razón", value=reason, inline=False)
        embed.set_footer(text=f"Usuario ID: {user.id} | Rol ID: {role.id}")
        
        await interaction.response.send_message(embed=embed)
        await send_log(interaction.guild, embed)
        
    except discord.Forbidden:
        await interaction.response.send_message("❌ No tengo permisos para quitar este rol.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error en removerole: {e}")
        await interaction.response.send_message(f"❌ Error al quitar el rol: {str(e)}", ephemeral=True)

@tree.command(name="roleinfo", description="Muestra información detallada sobre un rol")
@app_commands.describe(role="El rol a consultar")
async def roleinfo(interaction: discord.Interaction, role: discord.Role):
    if role.is_default():
        await interaction.response.send_message("❌ No puedes consultar información del rol @everyone.", ephemeral=True)
        return
    
    embed = discord.Embed(
        title=f"🎨 Información del Rol",
        color=role.color if role.color != discord.Color.default() else discord.Color.blue(),
        timestamp=datetime.now(timezone.utc)
    )
    
    embed.add_field(name="📋 Nombre", value=f"{role.mention}\n`{role.name}`", inline=True)
    embed.add_field(name="🆔 ID", value=f"`{role.id}`", inline=True)
    
    color_hex = f"#{role.color.value:06x}" if role.color.value != 0 else "Sin color"
    embed.add_field(name="🎨 Color", value=color_hex, inline=True)
    
    created_at = f"<t:{int(role.created_at.timestamp())}:F>\n<t:{int(role.created_at.timestamp())}:R>"
    embed.add_field(name="📅 Creado", value=created_at, inline=False)
    
    embed.add_field(name="📊 Posición", value=f"**{role.position}** / {len(interaction.guild.roles) - 1}", inline=True)
    embed.add_field(name="👥 Miembros", value=f"**{len(role.members)}** miembros", inline=True)
    
    features = []
    if role.hoist:
        features.append("📌 Mostrado separadamente")
    if role.mentionable:
        features.append("💬 Mencionable")
    if role.managed:
        features.append("🤖 Gestionado por integración")
    if role.is_premium_subscriber():
        features.append("💎 Rol de Boost")
    if role.is_bot_managed():
        features.append("🤖 Rol de bot")
    if role.is_integration():
        features.append("🔗 Rol de integración")
    
    if features:
        embed.add_field(name="✨ Características", value="\n".join(features), inline=True)
    
    perms = role.permissions
    important_perms = []
    
    if perms.administrator:
        important_perms.append("👑 **ADMINISTRADOR**")
    else:
        if perms.manage_guild:
            important_perms.append("⚙️ Gestionar servidor")
        if perms.manage_roles:
            important_perms.append("🎨 Gestionar roles")
        if perms.manage_channels:
            important_perms.append("📁 Gestionar canales")
        if perms.kick_members:
            important_perms.append("👢 Expulsar miembros")
        if perms.ban_members:
            important_perms.append("🔨 Banear miembros")
        if perms.moderate_members:
            important_perms.append("🛡️ Moderar miembros")
        if perms.manage_messages:
            important_perms.append("🗑️ Gestionar mensajes")
        if perms.mention_everyone:
            important_perms.append("📢 Mencionar @everyone")
    
    if important_perms:
        embed.add_field(name=f"🔐 Permisos clave ({len(important_perms)})", value="\n".join(important_perms[:15]), inline=False)
    else:
        embed.add_field(name="🔐 Permisos clave", value="Sin permisos especiales", inline=False)
    
    if 0 < len(role.members) <= 10:
        members_mentions = [m.mention for m in role.members[:10]]
        embed.add_field(name="👤 Algunos miembros", value=", ".join(members_mentions), inline=False)
    
    embed.set_footer(text=f"ID del rol: {role.id}")
    
    await interaction.response.send_message(embed=embed)

# ------------------Comandos de Información------------------#

@tree.command(name="userinfo", description="Muestra información detallada sobre un usuario")
@app_commands.describe(user="El usuario a consultar (opcional, por defecto tú mismo)")
async def userinfo(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    
    embed = discord.Embed(
        title=f"📋 Información de {target.name}",
        color=target.color if target.color != discord.Color.default() else discord.Color.blue(),
        timestamp=datetime.now(timezone.utc)
    )
    
    embed.set_thumbnail(url=target.display_avatar.url)
    if target.banner:
        embed.set_image(url=target.banner.url)
    
    embed.add_field(name="👤 Usuario", value=f"{target.mention}\n`{target.name}`\n`{target.id}`", inline=True)
    
    nickname = target.nick if target.nick else "Sin apodo"
    embed.add_field(name="🏷️ Apodo", value=nickname, inline=True)
    
    bot_status = "🤖 Bot" if target.bot else "👨‍💼 Usuario"
    embed.add_field(name="🎭 Tipo", value=bot_status, inline=True)
    
    created_at = f"<t:{int(target.created_at.timestamp())}:F>\n<t:{int(target.created_at.timestamp())}:R>"
    embed.add_field(name="📅 Cuenta creada", value=created_at, inline=False)
    
    joined_at = f"<t:{int(target.joined_at.timestamp())}:F>\n<t:{int(target.joined_at.timestamp())}:R>"
    embed.add_field(name="📥 Se unió al servidor", value=joined_at, inline=False)
    
    roles = [role.mention for role in target.roles if role.name != "@everyone"]
    if roles:
        roles_display = ", ".join(roles[:10])
        if len(roles) > 10:
            roles_display += f" *y {len(roles) - 10} más*"
    else:
        roles_display = "Sin roles"
    
    embed.add_field(name=f"🎨 Roles [{len(roles)}]", value=roles_display, inline=False)
    
    top_role = target.top_role.mention if target.top_role.name != "@everyone" else "Sin rol"
    embed.add_field(name="⭐ Rol más alto", value=top_role, inline=True)
    
    key_perms = []
    if target.guild_permissions.administrator:
        key_perms.append("👑 Administrador")
    if target.guild_permissions.manage_guild:
        key_perms.append("⚙️ Gestionar servidor")
    if target.guild_permissions.manage_channels:
        key_perms.append("📁 Gestionar canales")
    if target.guild_permissions.kick_members:
        key_perms.append("👢 Expulsar miembros")
    if target.guild_permissions.ban_members:
        key_perms.append("🔨 Banear miembros")
    if target.guild_permissions.moderate_members:
        key_perms.append("🛡️ Moderar miembros")
    
    perms_display = "\n".join(key_perms) if key_perms else "Sin permisos especiales"
    embed.add_field(name="🔐 Permisos clave", value=perms_display, inline=True)
    
    warns = get_user_warns_count(target.id, interaction.guild.id)
    if warns > 0:
        config = get_guild_config(interaction.guild.id)
        embed.add_field(name="⚠️ Warns", value=f"`{warns}/{config['max_warns']}`", inline=True)
    
    muted_role = discord.utils.get(interaction.guild.roles, name="Silenciado")
    if muted_role and muted_role in target.roles:
        mute_info = get_mute_info(target.id, interaction.guild.id)
        if mute_info:
            if mute_info['mute_duration'] is None:
                embed.add_field(name="🔇 Estado", value="**Muteado Permanentemente**", inline=True)
            else:
                mute_start = datetime.fromisoformat(mute_info['mute_start'])
                mute_end = mute_start + timedelta(seconds=mute_info['mute_duration'])
                time_left = mute_end - datetime.now(timezone.utc)
                if time_left.total_seconds() > 0:
                    embed.add_field(name="🔇 Estado", value=f"Muteado ({format_duration(int(time_left.total_seconds()))})", inline=True)
    
    embed.set_footer(text=f"ID: {target.id}")
    
    await interaction.response.send_message(embed=embed)

@tree.command(name="serverinfo", description="Muestra información detallada sobre el servidor")
async def serverinfo(interaction: discord.Interaction):
    guild = interaction.guild
    
    embed = discord.Embed(
        title=f"🏰 {guild.name}",
        description=guild.description if guild.description else "Sin descripción",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc)
    )
    
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    if guild.banner:
        embed.set_image(url=guild.banner.url)
    
    embed.add_field(name="🆔 ID del servidor", value=f"`{guild.id}`", inline=True)
    
    owner = guild.owner
    embed.add_field(name="👑 Dueño", value=f"{owner.mention}\n`{owner.name}`", inline=True)
    
    created_at = f"<t:{int(guild.created_at.timestamp())}:F>\n<t:{int(guild.created_at.timestamp())}:R>"
    embed.add_field(name="📅 Servidor creado", value=created_at, inline=True)
    
    verification_levels = {
        discord.VerificationLevel.none: "⚪ Ninguno",
        discord.VerificationLevel.low: "🟢 Bajo",
        discord.VerificationLevel.medium: "🟡 Medio",
        discord.VerificationLevel.high: "🟠 Alto",
        discord.VerificationLevel.highest: "🔴 Máximo"
    }
    embed.add_field(name="🛡️ Nivel de verificación", value=verification_levels.get(guild.verification_level, "Desconocido"), inline=True)
    
    content_filter = {
        discord.ContentFilter.disabled: "❌ Desactivado",
        discord.ContentFilter.no_role: "🔸 Sin rol",
        discord.ContentFilter.all_members: "✅ Todos"
    }
    embed.add_field(name="🔞 Filtro de contenido", value=content_filter.get(guild.explicit_content_filter, "Desconocido"), inline=True)
    
    boost_level = guild.premium_tier
    boost_count = guild.premium_subscription_count
    embed.add_field(name="💎 Nivel de Boost", value=f"Nivel {boost_level} ({boost_count} boosts)", inline=True)
    
    total_members = guild.member_count
    bots = sum(1 for member in guild.members if member.bot)
    humans = total_members - bots
    online = sum(1 for member in guild.members if member.status != discord.Status.offline and not member.bot)
    
    embed.add_field(
        name="👥 Miembros",
        value=f"Total: **{total_members}**\nHumanos: **{humans}**\nBots: **{bots}**\nEn línea: **{online}**",
        inline=True
    )
    
    text_channels = len(guild.text_channels)
    voice_channels = len(guild.voice_channels)
    categories = len(guild.categories)
    total_channels = text_channels + voice_channels
    
    embed.add_field(
        name="📁 Canales",
        value=f"Total: **{total_channels}**\nTexto: **{text_channels}**\nVoz: **{voice_channels}**\nCategorías: **{categories}**",
        inline=True
    )
    
    embed.add_field(name="🎨 Roles", value=f"**{len(guild.roles)}** roles", inline=True)
    
    total_emojis = len(guild.emojis)
    static_emojis = sum(1 for emoji in guild.emojis if not emoji.animated)
    animated_emojis = sum(1 for emoji in guild.emojis if emoji.animated)
    
    embed.add_field(
        name="😀 Emojis",
        value=f"Total: **{total_emojis}**\nEstáticos: **{static_emojis}**\nAnimados: **{animated_emojis}**",
        inline=True
    )
    
    embed.add_field(name="🎭 Stickers", value=f"**{len(guild.stickers)}** stickers", inline=True)
    
    features = []
    feature_map = {
        "COMMUNITY": "🏘️ Comunidad",
        "VERIFIED": "✅ Verificado",
        "PARTNERED": "🤝 Partner",
        "DISCOVERABLE": "🔍 Descubrible",
        "VANITY_URL": "🔗 URL personalizada",
        "ANIMATED_ICON": "🎬 Icono animado",
        "BANNER": "🖼️ Banner",
        "WELCOME_SCREEN_ENABLED": "👋 Bienvenida",
        "MEMBER_VERIFICATION_GATE_ENABLED": "🚪 Verificación",
    }
    
    for feature in guild.features:
        if feature in feature_map:
            features.append(feature_map[feature])
    
    if features:
        embed.add_field(name="✨ Características", value="\n".join(features[:10]), inline=False)
    
    embed.set_footer(text=f"Servidor ID: {guild.id}", icon_url=guild.icon.url if guild.icon else None)
    
    await interaction.response.send_message(embed=embed)

@tree.command(name="modlogs", description="Muestra el historial de moderación de un usuario")
@app_commands.describe(user="El usuario a consultar", limit="Cantidad de registros a mostrar (1-25)")
async def modlogs(interaction: discord.Interaction, user: discord.Member, limit: int = 10):
    if not interaction.user.guild_permissions.moderate_members:
        await interaction.response.send_message("❌ No tienes permisos para ver los logs de moderación.", ephemeral=True)
        return
    
    if limit < 1 or limit > 25:
        await interaction.response.send_message("❌ El límite debe estar entre 1 y 25.", ephemeral=True)
        return
    
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT action_type, moderator_id, reason, timestamp
                FROM mod_logs
                WHERE user_id = ? AND guild_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (user.id, interaction.guild.id, limit))
            
            logs = cursor.fetchall()
        
        if not logs:
            await interaction.response.send_message(f"ℹ️ No hay registros de moderación para {user.mention}.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title=f"📜 Historial de Moderación",
            description=f"Últimas {len(logs)} acciones de moderación para {user.mention}",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        
        action_emojis = {
            "WARN": "⚠️",
            "MUTE": "🔇",
            "UNMUTE": "🔓",
            "KICK": "👢",
            "BAN": "🔨",
            "UNBAN": "✅",
            "CLEAR_WARNS": "🗑️",
            "ADD_ROLE": "➕",
            "REMOVE_ROLE": "➖",
            "LOCKDOWN": "🔒",
            "UNLOCK": "🔓"
        }
        
        for i, (action_type, moderator_id, reason, timestamp) in enumerate(logs, 1):
            moderator = interaction.guild.get_member(moderator_id)
            mod_name = moderator.name if moderator else f"ID: {moderator_id}"
            
            emoji = action_emojis.get(action_type, "📝")
            time_str = f"<t:{int(datetime.fromisoformat(timestamp).timestamp())}:R>"
            
            embed.add_field(
                name=f"{emoji} {action_type}",
                value=f"**Moderador:** {mod_name}\n**Razón:** {reason}\n**Fecha:** {time_str}",
                inline=False
            )
        
        embed.set_footer(text=f"Total de registros: {len(logs)} | Usuario ID: {user.id}")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
    except Exception as e:
        logger.error(f"Error en modlogs: {e}")
        await interaction.response.send_message(f"❌ Error al consultar logs: {str(e)}", ephemeral=True)

# ------------------Comandos de Configuración------------------#

@tree.command(name="config", description="Configura el bot para este servidor")
@app_commands.describe(
    action="Acción a realizar",
    setting="Configuración a modificar",
    value="Valor a establecer"
)
@app_commands.choices(action=[
    app_commands.Choice(name="Ver configuración", value="view"),
    app_commands.Choice(name="Establecer valor", value="set"),
    app_commands.Choice(name="Restablecer valores", value="reset")
])
@app_commands.choices(setting=[
    app_commands.Choice(name="Canal de logs", value="log_channel"),
    app_commands.Choice(name="Máximo de warns", value="max_warns"),
    app_commands.Choice(name="Duración auto-mute", value="auto_mute_duration")
])
async def config(interaction: discord.Interaction, action: str, setting: str = None, value: str = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Solo administradores pueden configurar el bot.", ephemeral=True)
        return
    
    if action == "view":
        config_data = get_guild_config(interaction.guild.id)
        
        embed = discord.Embed(
            title="⚙️ Configuración del Servidor",
            description=f"Configuración actual para **{interaction.guild.name}**",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        
        log_channel = interaction.guild.get_channel(config_data['log_channel_id']) if config_data['log_channel_id'] else None
        embed.add_field(
            name="📝 Canal de Logs",
            value=log_channel.mention if log_channel else "No configurado",
            inline=True
        )
        
        embed.add_field(name="⚠️ Máximo de Warns", value=f"`{config_data['max_warns']}`", inline=True)
        embed.add_field(name="⏱️ Duración Auto-Mute", value=format_duration(config_data['auto_mute_duration']), inline=True)
        
        embed.set_footer(text="Usa /config set <opción> <valor> para modificar")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    elif action == "set":
        if not setting or not value:
            await interaction.response.send_message("❌ Debes especificar una configuración y un valor.", ephemeral=True)
            return
        
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                
                if setting == "log_channel":
                    # Verificar que sea un canal de texto válido
                    try:
                        channel_id = int(value.strip('<>#'))
                        channel = interaction.guild.get_channel(channel_id)
                        
                        if not channel or not isinstance(channel, discord.TextChannel):
                            await interaction.response.send_message("❌ Canal no válido o no es un canal de texto.", ephemeral=True)
                            return
                        
                        cursor.execute("""
                            INSERT INTO guild_config (guild_id, log_channel_id)
                            VALUES (?, ?)
                            ON CONFLICT(guild_id) DO UPDATE SET log_channel_id = ?
                        """, (interaction.guild.id, channel_id, channel_id))
                        
                        await interaction.response.send_message(f"✅ Canal de logs establecido a {channel.mention}", ephemeral=True)
                        
                    except ValueError:
                        await interaction.response.send_message("❌ ID de canal inválido.", ephemeral=True)
                        return
                
                elif setting == "max_warns":
                    try:
                        max_warns = int(value)
                        if max_warns < 1 or max_warns > 20:
                            await interaction.response.send_message("❌ El máximo de warns debe estar entre 1 y 20.", ephemeral=True)
                            return
                        
                        cursor.execute("""
                            INSERT INTO guild_config (guild_id, max_warns)
                            VALUES (?, ?)
                            ON CONFLICT(guild_id) DO UPDATE SET max_warns = ?
                        """, (interaction.guild.id, max_warns, max_warns))
                        
                        await interaction.response.send_message(f"✅ Máximo de warns establecido a `{max_warns}`", ephemeral=True)
                        
                    except ValueError:
                        await interaction.response.send_message("❌ Valor inválido. Debe ser un número.", ephemeral=True)
                        return
                
                elif setting == "auto_mute_duration":
                    duration_seconds = parse_time(value)
                    
                    if duration_seconds is None:
                        await interaction.response.send_message(
                            "❌ Formato de duración inválido.\n**Ejemplos:** `30m`, `2h`, `12h`, `1d`",
                            ephemeral=True
                        )
                        return
                    
                    if duration_seconds > 2592000:  # 30 días
                        await interaction.response.send_message("❌ La duración máxima es de 30 días.", ephemeral=True)
                        return
                    
                    cursor.execute("""
                        INSERT INTO guild_config (guild_id, auto_mute_duration)
                        VALUES (?, ?)
                        ON CONFLICT(guild_id) DO UPDATE SET auto_mute_duration = ?
                    """, (interaction.guild.id, duration_seconds, duration_seconds))
                    
                    await interaction.response.send_message(
                        f"✅ Duración de auto-mute establecida a `{format_duration(duration_seconds)}`",
                        ephemeral=True
                    )
                
        except Exception as e:
            logger.error(f"Error en config set: {e}")
            await interaction.response.send_message(f"❌ Error al guardar configuración: {str(e)}", ephemeral=True)
    
    elif action == "reset":
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM guild_config WHERE guild_id = ?", (interaction.guild.id,))
            
            await interaction.response.send_message("✅ Configuración restablecida a valores por defecto.", ephemeral=True)
            
        except Exception as e:
            logger.error(f"Error en config reset: {e}")
            await interaction.response.send_message(f"❌ Error al restablecer configuración: {str(e)}", ephemeral=True)

# ------------------Manejo de Errores------------------#

@warn.error
async def warn_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(
            f"⏳ Este comando está en cooldown. Intenta de nuevo en {error.retry_after:.1f}s",
            ephemeral=True
        )

@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Manejo global de errores de comandos"""
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(
            f"⏳ Este comando está en cooldown. Intenta de nuevo en {error.retry_after:.1f}s",
            ephemeral=True
        )
    elif isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            f"❌ Te faltan permisos para ejecutar este comando: {', '.join(error.missing_permissions)}",
            ephemeral=True
        )
    elif isinstance(error, app_commands.BotMissingPermissions):
        await interaction.response.send_message(
            f"❌ El bot no tiene los permisos necesarios: {', '.join(error.missing_permissions)}",
            ephemeral=True
        )
    elif isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message(
            "❌ No tienes permiso para usar este comando.",
            ephemeral=True
        )
    else:
        logger.error(f"Error en comando: {error}", exc_info=True)
        
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "❌ Ocurrió un error inesperado al ejecutar el comando.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                "❌ Ocurrió un error inesperado al ejecutar el comando.",
                ephemeral=True
            )

# ------------------Ejecuta el bot------------------#
try:
    logger.info("🚀 Iniciando bot...")
    client.run(token)
except discord.LoginFailure:
    logger.critical("❌ Token inválido. Verifica tu archivo .env")
except KeyboardInterrupt:
    logger.info("👋 Bot detenido manualmente")
except Exception as e:
    logger.critical(f"❌ Error crítico al iniciar el bot: {e}", exc_info=True)
