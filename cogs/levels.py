"""
cogs/levels.py
──────────────
Sistema de Niveles / XP.

Comandos:
  /rank [@usuario]         — Tarjeta de rango
  /leaderboard             — Top 10 del servidor
  /xp give @usuario N      — [admin] Dar XP manualmente
  /xp reset @usuario       — [admin] Resetear XP
  /xp config               — [admin] Panel de configuración (modal)
  /xp reward add nivel rol — [admin] Añadir recompensa de nivel
  /xp reward remove nivel  — [admin] Eliminar recompensa
  /xp reward list          — Ver recompensas configuradas

Fórmula: xp_para_nivel_N = 5N² + 50N + 100 (acumulado MEE6)
Anuncio: configurable (canal dedicado con mensaje persistente, o canal del usuario con auto-delete).
"""

import json
import logging
import asyncio
import random
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

MEDALS = ["🥇", "🥈", "🥉"]


def _xp_to_next_level(current_level: int) -> int:
    """XP necesario para pasar del nivel actual al siguiente."""
    n = current_level + 1
    return 5 * n * n + 50 * n + 100


def _xp_in_current_level(total_xp: int, current_level: int) -> int:
    """XP acumulado DENTRO del nivel actual (para la barra de progreso)."""
    from database.manager import DatabaseManager
    base = DatabaseManager._xp_for_level(current_level)
    return total_xp - base


def _progress_bar(current: int, total: int, length: int = 12) -> str:
    filled = int(length * current / total) if total > 0 else 0
    bar = "█" * filled + "░" * (length - filled)
    return f"`[{bar}]`"


# ── Modal de Configuración ────────────────────────────────────────────────────

class XPConfigModal(discord.ui.Modal, title="Configurar Sistema de XP"):
    xp_min = discord.ui.TextInput(label="XP mínimo por mensaje", default="15", max_length=5)
    xp_max = discord.ui.TextInput(label="XP máximo por mensaje", default="25", max_length=5)
    cooldown = discord.ui.TextInput(label="Cooldown entre mensajes (segundos)", default="60", max_length=6)
    announcement_msg = discord.ui.TextInput(
        label="Mensaje de subida de nivel (opcional)",
        style=discord.TextStyle.paragraph,
        placeholder="¡{user} ha subido al nivel {level}! 🎉",
        required=False,
        max_length=500,
    )

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        try:
            xp_min = int(self.xp_min.value)
            xp_max = int(self.xp_max.value)
            cooldown = int(self.cooldown.value)
        except ValueError:
            return await interaction.response.send_message("❌ Los valores deben ser números enteros.", ephemeral=True)

        if xp_min < 1 or xp_max < xp_min:
            return await interaction.response.send_message(
                "❌ XP mínimo debe ser ≥ 1 y XP máximo debe ser ≥ XP mínimo.", ephemeral=True
            )
        if cooldown < 0:
            return await interaction.response.send_message("❌ El cooldown debe ser ≥ 0.", ephemeral=True)

        kwargs = dict(xp_min=xp_min, xp_max=xp_max, cooldown_seconds=cooldown)
        if self.announcement_msg.value.strip():
            kwargs["announcement_message"] = self.announcement_msg.value.strip()

        self.cog.db.set_xp_config(interaction.guild_id, **kwargs)
        await interaction.response.send_message(
            f"✅ Configuración guardada:\n"
            f"- XP por mensaje: **{xp_min}–{xp_max}**\n"
            f"- Cooldown: **{cooldown}s**",
            ephemeral=True,
        )


class XPSetupView(discord.ui.View):
    """Panel interactivo de configuración de XP."""

    def __init__(self, cog, author_id: int):
        super().__init__(timeout=120)
        self.cog = cog
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Solo el ejecutor puede usar este panel.", ephemeral=True)
            return False
        return True

    def _build_embed(self, guild: discord.Guild) -> discord.Embed:
        cfg = self.cog.db.get_xp_config(guild.id)
        enabled = bool(cfg.get("enabled"))
        ann_ch_id = cfg.get("announcement_channel_id")
        ann_ch = guild.get_channel(int(ann_ch_id)) if ann_ch_id else None

        ignored = []
        try:
            ignored = json.loads(cfg.get("ignored_channels") or "[]")
        except Exception:
            pass

        embed = discord.Embed(
            title="⚙️ Configuración del Sistema XP",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Estado", value="✅ Activo" if enabled else "🔴 Inactivo", inline=True)
        embed.add_field(name="XP por mensaje", value=f"{cfg.get('xp_min', 15)}–{cfg.get('xp_max', 25)}", inline=True)
        embed.add_field(name="Cooldown", value=f"{cfg.get('cooldown_seconds', 60)}s", inline=True)
        embed.add_field(
            name="Canal de anuncios",
            value=ann_ch.mention if ann_ch else "Mismo canal del usuario",
            inline=True,
        )
        embed.add_field(
            name="Canales ignorados",
            value=f"{len(ignored)} canal(es)" if ignored else "Ninguno",
            inline=True,
        )
        embed.add_field(
            name="Apilar roles recompensa",
            value="✅ Sí" if cfg.get("stack_rewards", 1) else "❌ No (solo el último nivel)",
            inline=True,
        )
        embed.set_footer(text="Usa los botones para modificar la configuración")
        return embed

    @discord.ui.button(label="Activar/Desactivar", emoji="🔁", style=discord.ButtonStyle.primary, row=0)
    async def toggle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = self.cog.db.get_xp_config(interaction.guild_id)
        new_state = 0 if cfg.get("enabled") else 1
        self.cog.db.set_xp_config(interaction.guild_id, enabled=new_state)
        await interaction.response.edit_message(embed=self._build_embed(interaction.guild), view=self)

    @discord.ui.button(label="XP & Cooldown", emoji="📊", style=discord.ButtonStyle.secondary, row=0)
    async def xp_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(XPConfigModal(self.cog))

    @discord.ui.button(label="Canal de Anuncios", emoji="📣", style=discord.ButtonStyle.secondary, row=1)
    async def ann_channel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Menciona el canal de anuncios de nivel (escribe el nombre o @canal en el chat de este servidor).\n"
            "O usa `/xp config` → edita directamente con el slash command `/xp setannouncechannel`.",
            ephemeral=True,
        )

    @discord.ui.button(label="Apilar Roles", emoji="🔗", style=discord.ButtonStyle.secondary, row=1)
    async def stack_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = self.cog.db.get_xp_config(interaction.guild_id)
        new_val = 0 if cfg.get("stack_rewards", 1) else 1
        self.cog.db.set_xp_config(interaction.guild_id, stack_rewards=new_val)
        await interaction.response.edit_message(embed=self._build_embed(interaction.guild), view=self)

    @discord.ui.button(label="Cerrar", emoji="❌", style=discord.ButtonStyle.danger, row=2)
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="✅ Panel cerrado.", embed=None, view=None)
        self.stop()


# ── Cog ───────────────────────────────────────────────────────────────────────

class Levels(commands.Cog):
    """Sistema de niveles y XP con recompensas de roles."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db  # type: ignore
        # Cooldown en memoria: {(guild_id, user_id): timestamp_last_xp}
        self._xp_cooldown: dict = {}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        cfg = self.db.get_xp_config(message.guild.id)
        if not cfg.get("enabled"):
            return

        # Verificar canal ignorado
        try:
            ignored = json.loads(cfg.get("ignored_channels") or "[]")
        except Exception:
            ignored = []
        if message.channel.id in ignored:
            return

        # Verificar cooldown
        key = (message.guild.id, message.author.id)
        now_ts = datetime.now(timezone.utc).timestamp()
        cooldown = int(cfg.get("cooldown_seconds", 60))
        last_ts = self._xp_cooldown.get(key, 0)
        if now_ts - last_ts < cooldown:
            return

        self._xp_cooldown[key] = now_ts

        # Calcular XP con multiplicador de canal
        xp_min = int(cfg.get("xp_min", 15))
        xp_max = int(cfg.get("xp_max", 25))
        try:
            multipliers = json.loads(cfg.get("channel_multipliers") or "{}")
        except Exception:
            multipliers = {}
        multiplier = float(multipliers.get(str(message.channel.id), 1.0))
        xp_gained = int(random.randint(xp_min, xp_max) * multiplier)

        result = self.db.add_xp(message.author.id, message.guild.id, xp_gained)

        if result["leveled_up"]:
            await self._announce_levelup(message, result["level"], cfg)
            await self._assign_reward(message.author, message.guild, result["level"], cfg)

    async def _announce_levelup(self, message: discord.Message, new_level: int, cfg: dict):
        custom_msg = cfg.get("announcement_message") or "¡{user} ha subido al **nivel {level}**! 🎉"
        text = custom_msg.replace("{user}", message.author.mention).replace("{level}", str(new_level))

        ann_ch_id = cfg.get("announcement_channel_id")
        if ann_ch_id:
            channel = message.guild.get_channel(int(ann_ch_id))
            if channel and isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(text)
                    return
                except discord.Forbidden:
                    pass
        # Fallback: mismo canal con auto-delete
        try:
            await message.channel.send(text, delete_after=15)
        except discord.Forbidden:
            pass

    async def _assign_reward(self, member: discord.Member, guild: discord.Guild,
                              level: int, cfg: dict):
        reward = self.db.get_level_reward(guild.id, level)
        if not reward:
            return

        role = guild.get_role(int(reward["role_id"]))
        if not role:
            return

        stack = bool(cfg.get("stack_rewards", 1))
        try:
            if not stack:
                # Quitar roles de niveles anteriores
                all_rewards = self.db.get_level_rewards(guild.id)
                reward_role_ids = {int(r["role_id"]) for r in all_rewards if int(r["level"]) < level}
                roles_to_remove = [r for r in member.roles if r.id in reward_role_ids]
                if roles_to_remove:
                    await member.remove_roles(*roles_to_remove, reason=f"Nivel {level} — reemplazo de rol")
            await member.add_roles(role, reason=f"Recompensa nivel {level}")
        except discord.Forbidden:
            logger.warning(f"Sin permisos para asignar rol de nivel {level} en {guild.name}")

    # ── Slash Commands ────────────────────────────────────────────────────────

    @app_commands.command(name="rank", description="Muestra tu rango o el de otro usuario")
    @app_commands.describe(usuario="Usuario del que ver el rango (opcional)")
    async def rank(self, interaction: discord.Interaction, usuario: Optional[discord.Member] = None):
        target = usuario or interaction.user
        data = self.db.get_user_level(target.id, interaction.guild_id)

        total_xp = int(data["xp"])
        level = int(data["level"])
        xp_in_level = _xp_in_current_level(total_xp, level)
        xp_needed = _xp_to_next_level(level)
        bar = _progress_bar(xp_in_level, xp_needed)

        # Posición en el servidor
        leaderboard = self.db.get_leaderboard(interaction.guild_id, limit=9999)
        position = next((i + 1 for i, r in enumerate(leaderboard) if int(r["user_id"]) == target.id), "—")

        embed = discord.Embed(
            title=f"🏅 Rango de {target.display_name}",
            color=discord.Color.gold(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Nivel", value=f"**{level}**", inline=True)
        embed.add_field(name="XP Total", value=f"**{total_xp:,}**", inline=True)
        embed.add_field(name="Posición", value=f"**#{position}**", inline=True)
        embed.add_field(
            name=f"Progreso → Nivel {level + 1}",
            value=f"{bar} `{xp_in_level:,} / {xp_needed:,} XP`",
            inline=False,
        )
        embed.set_footer(text=f"Mensajes totales: {data['message_count']:,}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leaderboard", description="Top 10 del servidor por XP")
    async def leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        rows = self.db.get_leaderboard(interaction.guild_id, limit=10)
        if not rows:
            return await interaction.followup.send("📭 Nadie tiene XP en este servidor todavía.")

        embed = discord.Embed(
            title=f"🏆 Tabla de Clasificación — {interaction.guild.name}",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        lines = []
        for i, row in enumerate(rows):
            member = interaction.guild.get_member(int(row["user_id"]))
            name = member.display_name if member else f"ID:{row['user_id']}"
            medal = MEDALS[i] if i < 3 else f"`#{i + 1}`"
            lines.append(f"{medal} **{name}** — Nivel {row['level']} · {int(row['xp']):,} XP")

        embed.description = "\n".join(lines)
        embed.set_footer(text="Actualizado al momento de ejecutar el comando")
        await interaction.followup.send(embed=embed)

    xp_group = app_commands.Group(name="xp", description="Gestión del sistema XP")
    xp_reward_group = app_commands.Group(name="reward", description="Recompensas de nivel", parent=xp_group)

    @xp_group.command(name="give", description="[Admin] Da XP a un usuario")
    @app_commands.describe(usuario="Usuario al que dar XP", cantidad="Cantidad de XP a dar")
    @app_commands.checks.has_permissions(administrator=True)
    async def xp_give(self, interaction: discord.Interaction, usuario: discord.Member, cantidad: int):
        if cantidad <= 0:
            return await interaction.response.send_message("❌ La cantidad debe ser mayor que 0.", ephemeral=True)
        result = self.db.add_xp(usuario.id, interaction.guild_id, cantidad)
        await interaction.response.send_message(
            f"✅ **+{cantidad} XP** para {usuario.mention}. Ahora tiene **{result['xp']:,} XP** (Nivel {result['level']}).",
            ephemeral=True,
        )
        if result["leveled_up"]:
            cfg = self.db.get_xp_config(interaction.guild_id)
            await self._assign_reward(usuario, interaction.guild, result["level"], cfg)

    @xp_group.command(name="reset", description="[Admin] Resetea el XP de un usuario")
    @app_commands.describe(usuario="Usuario a resetear")
    @app_commands.checks.has_permissions(administrator=True)
    async def xp_reset(self, interaction: discord.Interaction, usuario: discord.Member):
        self.db.reset_user_level(usuario.id, interaction.guild_id)
        await interaction.response.send_message(
            f"✅ XP de {usuario.mention} reseteado a 0.", ephemeral=True
        )

    @xp_group.command(name="config", description="[Admin] Panel de configuración del sistema XP")
    @app_commands.checks.has_permissions(administrator=True)
    async def xp_config(self, interaction: discord.Interaction):
        view = XPSetupView(self, interaction.user.id)
        embed = view._build_embed(interaction.guild)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @xp_group.command(name="setannouncechannel", description="[Admin] Configura el canal de anuncios de nivel")
    @app_commands.describe(canal="Canal donde anunciar subidas de nivel (None para usar el canal del usuario)")
    @app_commands.checks.has_permissions(administrator=True)
    async def xp_set_announce(self, interaction: discord.Interaction,
                               canal: Optional[discord.TextChannel] = None):
        self.db.set_xp_config(
            interaction.guild_id,
            announcement_channel_id=canal.id if canal else None
        )
        if canal:
            await interaction.response.send_message(
                f"✅ Anuncios de nivel configurados en {canal.mention}.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "✅ Los anuncios aparecerán en el canal donde escribe el usuario (con auto-borrado en 15s).",
                ephemeral=True,
            )

    @xp_group.command(name="ignorechannel", description="[Admin] Ignora/des-ignora un canal para el XP")
    @app_commands.describe(canal="Canal a ignorar o des-ignorar")
    @app_commands.checks.has_permissions(administrator=True)
    async def xp_ignore_channel(self, interaction: discord.Interaction, canal: discord.TextChannel):
        cfg = self.db.get_xp_config(interaction.guild_id)
        try:
            ignored = json.loads(cfg.get("ignored_channels") or "[]")
        except Exception:
            ignored = []

        if canal.id in ignored:
            ignored.remove(canal.id)
            msg = f"✅ {canal.mention} ya **no** está ignorado para XP."
        else:
            ignored.append(canal.id)
            msg = f"✅ {canal.mention} ahora está **ignorado** para XP."

        self.db.set_xp_config(interaction.guild_id, ignored_channels=json.dumps(ignored))
        await interaction.response.send_message(msg, ephemeral=True)

    # ── Reward subcommands ─────────────────────────────────────────────────────

    @xp_reward_group.command(name="add", description="[Admin] Añade un rol de recompensa para un nivel")
    @app_commands.describe(nivel="Nivel en el que se entrega el rol", rol="Rol a entregar")
    @app_commands.checks.has_permissions(administrator=True)
    async def reward_add(self, interaction: discord.Interaction, nivel: int, rol: discord.Role):
        if nivel < 1:
            return await interaction.response.send_message("❌ El nivel debe ser ≥ 1.", ephemeral=True)
        self.db.set_level_reward(interaction.guild_id, nivel, rol.id)
        await interaction.response.send_message(
            f"✅ Recompensa añadida: al llegar al **nivel {nivel}** se entrega {rol.mention}.",
            ephemeral=True,
        )

    @xp_reward_group.command(name="remove", description="[Admin] Elimina la recompensa de un nivel")
    @app_commands.describe(nivel="Nivel cuya recompensa quieres eliminar")
    @app_commands.checks.has_permissions(administrator=True)
    async def reward_remove(self, interaction: discord.Interaction, nivel: int):
        reward = self.db.get_level_reward(interaction.guild_id, nivel)
        if not reward:
            return await interaction.response.send_message(
                f"❌ No hay ninguna recompensa configurada para el nivel **{nivel}**.", ephemeral=True
            )
        self.db.delete_level_reward(interaction.guild_id, nivel)
        await interaction.response.send_message(
            f"✅ Recompensa del nivel **{nivel}** eliminada.", ephemeral=True
        )

    @xp_reward_group.command(name="list", description="Lista todas las recompensas de nivel configuradas")
    async def reward_list(self, interaction: discord.Interaction):
        rewards = self.db.get_level_rewards(interaction.guild_id)
        if not rewards:
            return await interaction.response.send_message(
                "📭 No hay recompensas de nivel configuradas. Usa `/xp reward add` para crear una.",
                ephemeral=True,
            )

        embed = discord.Embed(
            title="🎁 Recompensas de Nivel",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        lines = []
        for r in rewards:
            role = interaction.guild.get_role(int(r["role_id"]))
            role_text = role.mention if role else f"Rol eliminado (ID:{r['role_id']})"
            lines.append(f"**Nivel {r['level']}** → {role_text}")

        embed.description = "\n".join(lines)
        cfg = self.db.get_xp_config(interaction.guild_id)
        embed.set_footer(text="Roles apilados: Sí" if cfg.get("stack_rewards", 1) else "Roles apilados: No (solo el último)")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Levels(bot))
