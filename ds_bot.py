import asyncio
import copy
import logging
import os
import re
import time
from typing import Optional, List, Tuple, Dict, Set

import discord
import spotipy
import yt_dlp
from aiogram.client.session import aiohttp
from discord import app_commands, Interaction, Role
from discord.ext import commands
from discord.ui import View, Button, Select, Modal, TextInput
from discord import ButtonStyle
from asyncio import Queue

from spotipy import SpotifyClientCredentials

# --- Инициализация бота ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True  # Включаем получение содержимого сообщений
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)
track_request_queue = asyncio.Queue()
tree = bot.tree



user_message_history = {}
SPAM_THRESHOLD = 5  # сообщений
SPAM_TIME_WINDOW = 4  # секунд
MUTE_DURATION = 2 * 60 * 60  # 2 часа в секундах

user_temp_vcs = {}  # (guild_id, user_id) -> vc_id
server_settings = {}
log_channels = {}
balances = {}  # {user_id: coins}
shop_items = []  # [{'name': ..., 'price': ..., 'description': ...}]




# ---------------- Хранение ролей админов ----------------
admin_roles = {}  # guild_id -> [role_id, role_id, ...]

# ---------------- Проверка is_admin ----------------
def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        guild_id = interaction.guild.id
        user = interaction.user

        if user.id == interaction.guild.owner_id:
            return True

        if guild_id in admin_roles:
            user_roles = [role.id for role in user.roles]
            for r in admin_roles[guild_id]:
                if r in user_roles:
                    return True

        return False
    return app_commands.check(predicate)

# ---------------- Set Admin Roles ----------------
@bot.tree.command(name="set_admin_roles", description="Задать роли для админ-команд")
@app_commands.describe(role_names="Через запятую укажите роли для админ-команд")
async def set_admin_roles(interaction: discord.Interaction, role_names: str):
    if interaction.user.id != interaction.guild.owner_id:
        return await interaction.response.send_message(
            "❌ Только владелец сервера может использовать эту команду.", ephemeral=True
        )

    role_names_list = [name.strip() for name in role_names.split(",")]
    roles = []
    for name in role_names_list:
        role = discord.utils.get(interaction.guild.roles, name=name)
        if role:
            roles.append(role)

    if not roles:
        return await interaction.response.send_message("⚠️ Не найдено ни одной роли.", ephemeral=True)

    admin_roles[interaction.guild.id] = [role.id for role in roles]
    await interaction.response.send_message(
        f"✅ Роли для админ-команд установлены: {', '.join(role.mention for role in roles)}"
    )

# --- ХЕЛПЕР ДЛЯ EMBED ---
def build_recruitment_embed(guild: discord.Guild | None) -> discord.Embed:
    desc = (
        "Мы ищем новых ребят в нашу команду 🌟\n\n"
        "Хотите быть хелпером, ведущим или креативщиком ивентов?\n"
        "Заполняйте заявку — и у вас будет шанс присоединиться к нам!\n\n"
        "Выберите должность в меню ниже и расскажите немного о себе ✨"
    )

    embed = discord.Embed(
        title="💫 Привет, друзья!",
        description=desc,
        color=discord.Color.blurple()
    )
    if guild and guild.icon:
        try:
            embed.set_thumbnail(url=guild.icon.url)
        except Exception:
            pass
    embed.set_footer(text="Набор открыт")
    embed.timestamp = discord.utils.utcnow()
    return embed


# ---------- Модалка заявки ----------
class ApplicationModal(Modal, title="Заявка на должность"):
    def __init__(self, role_name: str, target_channel_id: Optional[int]):
        super().__init__(timeout=None)
        self.role_name = role_name
        self.target_channel_id = target_channel_id

        self.reason = TextInput(
            label="Информация о Вас",
            placeholder="Имя, возраст, часовой пояс, почему именно вы?",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1500
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("❌ Эту форму можно отправлять только на сервере.", ephemeral=True)

        admin_channel: Optional[discord.TextChannel] = None
        if self.target_channel_id:
            ch = interaction.guild.get_channel(self.target_channel_id)
            if isinstance(ch, discord.TextChannel):
                admin_channel = ch

        if admin_channel is None:
            admin_channel = discord.utils.get(interaction.guild.text_channels, name="admin-channel")

        if not admin_channel:
            return await interaction.response.send_message(
                "❌ Ошибка: не удалось найти целевой канал для заявок.",
                ephemeral=True
            )

        desc = (
            f"**Пользователь:** {interaction.user.mention}\n"
            f"**Должность:** {self.role_name}\n"
            f"**Причина:** {self.reason.value}"
        )
        if len(desc) > 4000:
            desc = desc[:3990] + "…"

        embed = discord.Embed(
            title="📩 Новая заявка",
            description=desc,
            color=discord.Color.green()
        )
        embed.set_footer(text=f"ID: {interaction.user.id}")

        await admin_channel.send(embed=embed)
        await interaction.response.send_message("✅ Ваша заявка отправлена администрации!", ephemeral=True)


# ---------- Кастомный Select ----------
class RoleSelect(Select):
    def __init__(self, roles_with_desc: List[Tuple[str, str]], target_channel_id: Optional[int]):
        clean: List[Tuple[str, str]] = []
        for role, desc in roles_with_desc:
            role = (role or "").strip()
            desc = (desc or "Без описания").strip() or "Без описания"
            if not role:
                continue
            clean.append((role[:100], desc[:100]))

        if not clean:
            raise ValueError("Нельзя создать селектор без ролей!")

        self.target_channel_id = target_channel_id
        options = [discord.SelectOption(label=role, description=desc) for role, desc in clean]
        super().__init__(
            placeholder="Выберите должность",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="application_select"
        )

    async def callback(self, interaction: discord.Interaction):
        selected_role = self.values[0]
        await interaction.response.send_modal(ApplicationModal(role_name=selected_role, target_channel_id=self.target_channel_id))


# ---------- View ----------
class ApplicationView(View):
    def __init__(self, roles_with_desc: List[Tuple[str, str]], target_channel_id: Optional[int]):
        super().__init__(timeout=None)
        self.add_item(RoleSelect(roles_with_desc, target_channel_id))


# ---------- Модалка настройки панели ----------
class ApplicationSetupModal(Modal, title="Создание панели заявок"):
    def __init__(self, target_channel_id: Optional[int]):
        super().__init__(timeout=None)
        self.target_channel_id = target_channel_id
        self.roles_input = TextInput(
            label="Роли и описания",
            placeholder="Хелпер | Помогать участникам\nВедущий трибун | Вести мероприятия\nИвент мейкер | Делать конкурсы",
            style=discord.TextStyle.paragraph,
            required=True
        )
        self.add_item(self.roles_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.roles_input.value or ""
        lines = [line.strip() for line in raw.split("\n") if line.strip()]
        roles_with_desc: List[Tuple[str, str]] = []

        for line in lines:
            if "|" in line:
                role, desc = line.split("|", 1)
            else:
                role, desc = line, "Без описания"
            role = (role or "").strip()
            desc = (desc or "Без описания").strip() or "Без описания"
            if role:
                roles_with_desc.append((role, desc))

        if not roles_with_desc:
            return await interaction.response.send_message(
                "⚠️ Не удалось распознать роли. Введите хотя бы одну корректную роль.",
                ephemeral=True
            )

        try:
            view = ApplicationView(roles_with_desc, self.target_channel_id)
            embed = build_recruitment_embed(interaction.guild)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
        except ValueError as e:
            await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.response.send_message(f"❌ Discord отказал: {e}", ephemeral=True)



# Ожидаем, что bot уже создан в твоём файле:
# bot = commands.Bot(command_prefix="!", intents=intents)
# tree = bot.tree

# --------------------------
#  Хранилища
# --------------------------
MOD_ROLE_RANKS: Dict[int, Dict[int, int]] = {}  # guild_id -> {role_id: rank}
LOCK_SNAPSHOTS: Dict[int, Dict[int, Dict[str, dict]]] = {}  # guild_id -> channel_id -> snapshot

# --------------------------
#  Утилиты рангов
# --------------------------
def _get_member_rank(member: discord.Member) -> int:
    """Макс. ранг по ролям участника (0 если нет). Владелец сервера = 99."""
    if member.id == member.guild.owner_id:
        return 99
    ranks = MOD_ROLE_RANKS.get(member.guild.id, {})
    max_rank = 0
    for role in getattr(member, "roles", []):
        if isinstance(role, discord.Role):
            r = int(ranks.get(role.id, 0))
            if r > max_rank:
                max_rank = r
    return max_rank

def requires_rank(min_rank: int):
    """Декоратор: доступ по минимальному рангу (или владелец сервера)."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        if interaction.user.id == interaction.guild.owner_id:
            return True
        return _get_member_rank(interaction.user) >= int(min_rank)
    return app_commands.check(predicate)

# --------------------------
#  Настройка рангов (только владелец)
# --------------------------
@bot.tree.command(name="set_role_rank", description="(Владелец) Задать ранг роли для мод-команд (0-3)")
@app_commands.describe(
    role="Роль (упоминание/ID/имя)",
    rank="0 = снять; 1 = warn; 2 = mute/unmute (+ ниже); 3 = ban/unban (+ ниже)"
)
async def set_role_rank(interaction: discord.Interaction, role: str, rank: int):
    if interaction.user.id != interaction.guild.owner_id:
        return await interaction.response.send_message("❌ Только владелец сервера.", ephemeral=True)
    if rank < 0 or rank > 3:
        return await interaction.response.send_message("❌ Ранг должен быть от 0 до 3.", ephemeral=True)

    # парсинг роли: @mention, ID, имя
    r: Optional[discord.Role] = None
    m = re.fullmatch(r"<@&(\d+)>", role.strip())
    if m:
        r = interaction.guild.get_role(int(m.group(1)))
    elif role.isdigit():
        r = interaction.guild.get_role(int(role))
    else:
        r = discord.utils.get(interaction.guild.roles, name=role)

    if not r:
        return await interaction.response.send_message("❌ Роль не найдена.", ephemeral=True)

    gmap = MOD_ROLE_RANKS.setdefault(interaction.guild.id, {})
    if rank == 0:
        gmap.pop(r.id, None)
        msg = f"🗑 Ранг снят с роли {r.mention}."
    else:
        gmap[r.id] = rank
        msg = f"✅ Для роли {r.mention} установлен ранг **{rank}**."

    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="list_role_ranks", description="(Владелец) Показать ранги ролей для мод-команд")
async def list_role_ranks(interaction: discord.Interaction):
    if interaction.user.id != interaction.guild.owner_id:
        return await interaction.response.send_message("❌ Только владелец сервера.", ephemeral=True)

    gmap = MOD_ROLE_RANKS.get(interaction.guild.id, {})
    if not gmap:
        return await interaction.response.send_message("ℹ️ Ранги ещё не назначены.", ephemeral=True)

    lines = []
    for rid, rank in sorted(gmap.items(), key=lambda x: (x[1], x[0]), reverse=True):
        role = interaction.guild.get_role(rid)
        if role:
            title = {1: "warn", 2: "mute/unmute", 3: "ban/unban"}.get(rank, "—")
            lines.append(f"{role.mention} → ранг **{rank}** ({title})")
    await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

# --------------------------
#  Команда WARN (R1)
# --------------------------
@bot.tree.command(name="warn", description="Выдать предупреждение пользователю")
@requires_rank(1)
@app_commands.describe(user="Кому выдать предупреждение", reason="Причина (необязательно)")
async def warn_cmd(interaction: discord.Interaction, user: discord.Member, reason: Optional[str] = None):
    await interaction.response.send_message(
        f"⚠️ {user.mention} получил предупреждение. Причина: {reason or 'не указана'}",
        ephemeral=True
    )
    # тут можешь добавить логирование в канал, БД и т.п.

# --------------------------
#  Команды MUTE / UNMUTE (R2)
# --------------------------
async def setup_muted_role(guild: discord.Guild) -> discord.Role:
    role = discord.utils.get(guild.roles, name="Muted")
    if role is None:
        role = await guild.create_role(name="Muted", reason="Роль для мута")
        for ch in guild.channels:
            try:
                await ch.set_permissions(role, send_messages=False, add_reactions=False, connect=False, speak=False)
            except (discord.Forbidden, discord.HTTPException):
                pass
    return role

@bot.tree.command(name="mute", description="Замьютить участника на N минут")
@requires_rank(2)
@app_commands.describe(member="Кого замьютить", minutes="На сколько минут (по умолчанию 10)", reason="Причина (необязательно)")
async def mute_cmd(interaction: discord.Interaction, member: discord.Member, minutes: Optional[int] = 10, reason: Optional[str] = None):
    role = await setup_muted_role(interaction.guild)
    try:
        await member.add_roles(role, reason=reason or f"Mute {minutes}m by {interaction.user}")
        await interaction.response.send_message(f"🔇 {member.mention} замьючен на {minutes} мин.", ephemeral=True)
    except discord.Forbidden:
        return await interaction.response.send_message("❌ Нет прав выдать мут.", ephemeral=True)

    async def unmute_after():
        await asyncio.sleep(max(1, int(minutes)) * 60)
        try:
            await member.remove_roles(role, reason="Mute expired")
        except Exception:
            pass
    asyncio.create_task(unmute_after())

@bot.tree.command(name="unmute", description="Снять мут с участника")
@requires_rank(2)
@app_commands.describe(member="С кого снять мут", reason="Причина (необязательно)")
async def unmute_cmd(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None):
    role = discord.utils.get(interaction.guild.roles, name="Muted")
    if role is None or role not in member.roles:
        return await interaction.response.send_message("ℹ️ Этот участник не замьючен.", ephemeral=True)
    try:
        await member.remove_roles(role, reason=reason or f"Unmute by {interaction.user}")
        await interaction.response.send_message(f"🔈 Мут снят с {member.mention}.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Нет прав снять мут.", ephemeral=True)

# --------------------------
#  Команды BAN / UNBAN (R3)
# --------------------------
@bot.tree.command(name="ban", description="Забанить пользователя")
@requires_rank(3)
@app_commands.describe(
    user="Кого забанить",
    reason="Причина (необязательно)",
    delete_message_days="Удалить сообщения за N дней (0–7)"
)
async def ban_cmd(interaction: discord.Interaction, user: discord.User, reason: Optional[str] = None, delete_message_days: Optional[int] = 0):
    delete_message_days = max(0, min(7, int(delete_message_days or 0)))
    try:
        await interaction.guild.ban(
            user,
            reason=reason or f"Ban by {interaction.user}",
            delete_message_days=delete_message_days
        )
        await interaction.response.send_message(f"⛔ Забанен: **{user}**. Причина: {reason or 'не указана'}", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Нет прав забанить этого пользователя.", ephemeral=True)
    except discord.HTTPException as e:
        await interaction.response.send_message(f"❌ Ошибка Discord API: {e}", ephemeral=True)

@bot.tree.command(name="unban", description="Снять бан с пользователя (по ID или name#tag)")
@requires_rank(3)
@app_commands.describe(query="ID или имя#тег (пример: 123456789012345678 или Name#0001)")
async def unban_cmd(interaction: discord.Interaction, query: str):
    bans = await interaction.guild.bans()
    target_entry = None

    if query.isdigit():  # как ID
        uid = int(query)
        for e in bans:
            if e.user.id == uid:
                target_entry = e
                break
    if not target_entry and "#" in query:  # как name#discrim
        name, discrim = query.rsplit("#", 1)
        for e in bans:
            if e.user.name == name and e.user.discriminator == discrim:
                target_entry = e
                break

    if not target_entry:
        return await interaction.response.send_message("❌ Пользователь в бан-листе не найден.", ephemeral=True)

    try:
        await interaction.guild.unban(target_entry.user, reason=f"Unban by {interaction.user}")
        await interaction.response.send_message(f"✅ Разбанен: **{target_entry.user}**", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Нет прав снять бан.", ephemeral=True)
    except discord.HTTPException as e:
        await interaction.response.send_message(f"❌ Ошибка Discord API: {e}", ephemeral=True)

# --------------------------
#  Закрыть / открыть чат (только владелец)
# --------------------------
def _ensure_snapshot(guild_id: int):
    return LOCK_SNAPSHOTS.setdefault(guild_id, {})

def _get_channel_snapshot(guild_id: int, channel_id: int):
    return LOCK_SNAPSHOTS.get(guild_id, {}).get(channel_id)

@bot.tree.command(name="lock_chat", description="(Владелец) Закрыть чат для всех, с сохранением и восстановлением прав")
@app_commands.describe(channel="Канал (по умолчанию — текущий)")
async def lock_chat(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    if interaction.user.id != interaction.guild.owner_id:
        return await interaction.response.send_message("❌ Только владелец сервера.", ephemeral=True)

    ch = channel or interaction.channel
    if not isinstance(ch, discord.TextChannel):
        return await interaction.response.send_message("❌ Это не текстовый канал.", ephemeral=True)

    perms = ch.permissions_for(interaction.guild.me)
    if not perms.manage_roles or not perms.manage_channels:
        return await interaction.response.send_message("❌ Нужны права на управление ролями/каналами.", ephemeral=True)

    if _get_channel_snapshot(interaction.guild.id, ch.id):
        return await interaction.response.send_message("ℹ️ Этот канал уже закрыт этой командой.", ephemeral=True)

    snapshot_roles: Dict[int, Optional[bool]] = {}
    snapshot_members: Dict[int, Optional[bool]] = {}
    everyone = ch.overwrites_for(interaction.guild.default_role).send_messages

    for target, ow in ch.overwrites.items():
        if isinstance(target, discord.Role):
            snapshot_roles[target.id] = ow.send_messages
        elif isinstance(target, discord.Member):
            snapshot_members[target.id] = ow.send_messages

    _ensure_snapshot(interaction.guild.id)[ch.id] = {
        "roles": snapshot_roles,
        "members": snapshot_members,
        "everyone": everyone,
    }

    # закрываем отправку всем
    ow_every = ch.overwrites_for(interaction.guild.default_role)
    ow_every.send_messages = False
    await ch.set_permissions(interaction.guild.default_role, overwrite=ow_every)

    for target in list(ch.overwrites.keys()):
        current = ch.overwrites_for(target)
        if current.send_messages is not False:
            current.send_messages = False
            try:
                await ch.set_permissions(target, overwrite=current)
            except (discord.Forbidden, discord.HTTPException):
                pass

    await interaction.response.send_message(f"🔒 Канал {ch.mention} закрыт для отправки сообщений.", ephemeral=True)

@bot.tree.command(name="unlock_chat", description="(Владелец) Открыть чат и восстановить прежние права")
@app_commands.describe(channel="Канал (по умолчанию — текущий)")
async def unlock_chat(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    if interaction.user.id != interaction.guild.owner_id:
        return await interaction.response.send_message("❌ Только владелец сервера.", ephemeral=True)

    ch = channel or interaction.channel
    if not isinstance(ch, discord.TextChannel):
        return await interaction.response.send_message("❌ Это не текстовый канал.", ephemeral=True)

    snap = _get_channel_snapshot(interaction.guild.id, ch.id)
    if not snap:
        return await interaction.response.send_message("ℹ️ Для этого канала нет сохранённых прав (не закрывали).", ephemeral=True)

    perms = ch.permissions_for(interaction.guild.me)
    if not perms.manage_roles or not perms.manage_channels:
        return await interaction.response.send_message("❌ Нужны права на управление ролями/каналами.", ephemeral=True)

    # восстановим @everyone
    prev_every = snap.get("everyone", None)
    owe = ch.overwrites_for(interaction.guild.default_role)
    owe.send_messages = prev_every
    try:
        await ch.set_permissions(interaction.guild.default_role, overwrite=owe)
    except (discord.Forbidden, discord.HTTPException):
        pass

    # восстановим роли
    for rid, prev in snap.get("roles", {}).items():
        role = interaction.guild.get_role(rid)
        if not role:
            continue
        ow = ch.overwrites_for(role)
        ow.send_messages = prev
        try:
            await ch.set_permissions(role, overwrite=ow)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # восстановим участникам
    for uid, prev in snap.get("members", {}).items():
        member = interaction.guild.get_member(uid)
        if not member:
            continue
        ow = ch.overwrites_for(member)
        ow.send_messages = prev
        try:
            await ch.set_permissions(member, overwrite=ow)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # очистим снапшот
    try:
        del LOCK_SNAPSHOTS[interaction.guild.id][ch.id]
        if not LOCK_SNAPSHOTS[interaction.guild.id]:
            del LOCK_SNAPSHOTS[interaction.guild.id]
    except KeyError:
        pass

    await interaction.response.send_message(f"🔓 Канал {ch.mention} открыт, права восстановлены.", ephemeral=True)

# --------------------------
#  Общий обработчик ошибок ранга
# --------------------------
@warn_cmd.error
@mute_cmd.error
@unmute_cmd.error
@ban_cmd.error
@unban_cmd.error
async def _rank_check_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("❌ Недостаточный ранг для этой команды.", ephemeral=True)






# ---------- Команда /заявки ----------
@bot.tree.command(name="заявки", description="Создать панель заявок")
@app_commands.describe(channel="Канал, куда будут приходить заявки")
@is_admin()
async def заявки(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    target_channel_id = channel.id if channel else None
    await interaction.response.send_modal(ApplicationSetupModal(target_channel_id))


@заявки.error
async def заявки_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        if interaction.response.is_done():
            await interaction.followup.send("❌ У вас нет доступа к этой команде.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ У вас нет доступа к этой команде.", ephemeral=True)


import os, re, shlex, asyncio, logging, copy
from typing import Optional, List, Tuple, Dict, Any

import aiohttp
import discord
from discord import app_commands, Interaction
from discord.ext import commands
from discord.ui import View, Button
from discord import ButtonStyle
import yt_dlp

# ============================ НАСТРОЙКИ ============================
FFMPEG_BIN = "/usr/bin/ffmpeg"        # системный ffmpeg
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# yt-dlp базовые опции (YouTube-first, webm/opus в приоритете)
YTDLP_BASE = {
    "format": "bestaudio[ext=webm][acodec=opus]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "source_address": "0.0.0.0",  # IPv4
    "extract_flat": False,
    "default_search": "ytsearch",
    "extractor_args": {"youtube": {"player_client": ["android", "android_music", "web_safari"]}},
    "http_headers": {"User-Agent": DEFAULT_UA},
}

# автодополнение
AUTOCOMPLETE_TIMEOUT = 1.5
SUGG_TTL = 120  # сек
_USE_DEEZER = False  # True — deezer в приоритете; False — iTunes

# ============================ УТИЛИТЫ ============================
def is_url(s: str) -> bool:
    return bool(re.match(r"^https?://", s or "", re.I))

async def ytdlp_extract(q: str, opts: Optional[dict] = None, timeout: float = 12.0) -> Optional[Dict[str, Any]]:
    """Запуск yt-dlp в отдельном потоке с таймаутом."""
    loop = asyncio.get_running_loop()
    merged = copy.deepcopy(YTDLP_BASE)
    if opts:
        # мягко обновляем, чтобы базовые ключи оставались
        for k, v in opts.items():
            merged[k] = v

    def _run():
        with yt_dlp.YoutubeDL(merged) as ydl:
            return ydl.extract_info(q, download=False)

    try:
        return await asyncio.wait_for(loop.run_in_executor(None, _run), timeout=timeout)
    except Exception as e:
        logging.warning("yt-dlp error for %r: %s", q, e)
        return None

def headers_to_crlf(headers: Dict[str, str]) -> str:
    return "".join(f"{k}: {v}\r\n" for k, v in headers.items() if v)

def build_ffmpeg_kwargs(headers: Dict[str, str]) -> dict:
    h = dict(headers or {})
    h.setdefault("User-Agent", DEFAULT_UA)
    h.setdefault("Accept", "*/*")
    hdr = headers_to_crlf(h)
    before = (
        "-nostdin "
        "-reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 "
        "-reconnect_at_eof 1 -reconnect_delay_max 5 "
        f"-headers {shlex.quote(hdr)} "
        "-protocol_whitelist file,crypto,http,https,tcp,tls "
        "-rw_timeout 20000000 "            # 20s (микросекунды)
        "-fflags +nobuffer -flags low_delay "
        "-probesize 64k -analyzeduration 0 "
    )
    return {
        "before_options": before,
        "options": "-vn -sn -ar 48000 -ac 2 -loglevel error",
        "executable": FFMPEG_BIN,
    }

# ============================ АВТОДОПОЛНЕНИЕ ============================
_SUGG_CACHE: Dict[str, Tuple[float, List[app_commands.Choice[str]]]] = {}

def _cache_get(q: str) -> Optional[List[app_commands.Choice[str]]]:
    key = q.lower()
    item = _SUGG_CACHE.get(key)
    if not item:
        return None
    ts, data = item
    if (asyncio.get_event_loop().time() - ts) > SUGG_TTL:
        return None
    return data

def _cache_put(q: str, data: List[app_commands.Choice[str]]) -> None:
    _SUGG_CACHE[q.lower()] = (asyncio.get_event_loop().time(), data)

async def itunes_autocomplete(current: str) -> List[app_commands.Choice[str]]:
    q = (current or "").strip()
    if len(q) < 2:
        return []
    cached = _cache_get(q)
    if cached is not None:
        return cached

    url = "https://itunes.apple.com/search"
    params = {"term": q, "entity": "song", "limit": 10, "country": "US"}
    timeout = aiohttp.ClientTimeout(total=AUTOCOMPLETE_TIMEOUT)
    out: List[app_commands.Choice[str]] = []
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url, params=params) as r:
                if r.status != 200:
                    _cache_put(q, [])
                    return []
                data = await r.json()
                for item in data.get("results", [])[:10]:
                    title = (item.get("trackName") or item.get("collectionName") or "Без названия").strip()
                    artist = (item.get("artistName") or "").strip()
                    label = f"{title} — {artist}" if artist else title
                    # value — строка для ytsearch
                    out.append(app_commands.Choice(name=label[:100], value=f"{title} {artist} audio"))
    except Exception:
        pass

    _cache_put(q, out)
    return out

async def deezer_autocomplete(current: str) -> List[app_commands.Choice[str]]:
    q = (current or "").strip()
    if len(q) < 2:
        return []
    cached = _cache_get(q)
    if cached is not None:
        return cached

    url = "https://api.deezer.com/search"
    params = {"q": q, "limit": 10}
    timeout = aiohttp.ClientTimeout(total=AUTOCOMPLETE_TIMEOUT)
    out: List[app_commands.Choice[str]] = []
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url, params=params) as r:
                if r.status != 200:
                    _cache_put(q, [])
                    return []
                data = await r.json()
                for item in (data.get("data") or [])[:10]:
                    title = (item.get("title") or "Без названия").strip()
                    artist = ((item.get("artist") or {}).get("name") or "").strip()
                    label = f"{title} — {artist}" if artist else title
                    out.append(app_commands.Choice(name=label[:100], value=f"{title} {artist} audio"))
    except Exception:
        pass

    _cache_put(q, out)
    return out

async def yt_autocomplete(current: str) -> List[app_commands.Choice[str]]:
    q = (current or "").strip()
    if len(q) < 2:
        return []
    info = await ytdlp_extract(f"ytsearch10:{q}", {"extract_flat": True}, timeout=7.0)
    if not info or "entries" not in info:
        return []
    out: List[app_commands.Choice[str]] = []
    for e in (info.get("entries") or []):
        title = e.get("title") or "Unknown"
        url = e.get("url") or e.get("webpage_url")
        if not url:
            continue
        out.append(app_commands.Choice(name=title[:100], value=url))
    return out

async def smart_autocomplete(current: str) -> List[app_commands.Choice[str]]:
    primary = await (deezer_autocomplete(current) if _USE_DEEZER else itunes_autocomplete(current))
    if primary:
        return primary
    return await yt_autocomplete(current)

# ============================ ПЛЕЕР ============================
Track = Tuple[str, str, Optional[str], Optional[str], Dict[str, str]]  # title, stream, thumb, page, http_headers

class MusicPlayer:
    """На сервер — один экземпляр."""
    def __init__(self, guild: discord.Guild, vc: discord.VoiceClient,
                 text_channel: discord.abc.Messageable, bot: discord.Client):
        self.guild = guild
        self.vc = vc
        self.text_channel = text_channel
        self.bot = bot
        self.volume = 0.75
        self.queue: List[Track] = []
        self.current: Optional[Track] = None
        self.current_source: Optional[discord.AudioSource] = None
        self._lock = asyncio.Lock()
        self._leave_guard = False
        self.control_message: Optional[discord.Message] = None

    async def play_next(self):
        async with self._lock:
            if not self.queue:
                await self._grace_and_leave()
                return

            title, stream_url, thumb, page, headers = self.queue.pop(0)
            self.current = (title, stream_url, thumb, page, headers)

            ffmpeg_kwargs = build_ffmpeg_kwargs(headers)
            try:
                base = discord.FFmpegPCMAudio(stream_url, **ffmpeg_kwargs)
            except Exception as e:
                logging.error("FFmpeg init error: %s", e)
                await self.play_next()
                return

            source = discord.PCMVolumeTransformer(base, volume=self.volume)
            self.current_source = source

            def _after(err: Optional[Exception]):
                if err:
                    logging.error("Playback error: %s", err)
                fut = asyncio.run_coroutine_threadsafe(self.play_next(), self.bot.loop)
                try: fut.result()
                except Exception as e: logging.error("after future error: %s", e)

            try:
                if not self.vc or not self.vc.is_connected():
                    logging.warning("VoiceClient disconnected before play.")
                    return
                self.vc.play(source, after=_after)
            except Exception as e:
                logging.error("vc.play error: %s", e)
                await self.play_next()
                return

            await self.update_panel()

    async def _grace_and_leave(self):
        if self._leave_guard:
            return
        self._leave_guard = True
        await asyncio.sleep(8)  # подождём — вдруг добавят трек
        self._leave_guard = False

        if self.queue or (self.vc and (self.vc.is_playing() or self.vc.is_paused())):
            return
        try:
            if self.vc and self.vc.is_connected():
                await self.vc.disconnect(force=True)
        except Exception:
            pass
        self.current = None
        self.current_source = None
        self.control_message = None

    async def update_panel(self):
        title = self.current[0] if self.current else "Нет трека"
        status = "▶️ Воспроизведение" if self.vc and self.vc.is_playing() else ("⏸ Пауза" if self.vc and self.vc.is_paused() else "❌ Не играет")
        qtxt = "\n".join(f"{i+1}. {t[0]}" for i, t in enumerate(self.queue[:10])) or "Очередь пуста"
        vol = int(self.volume * 100)

        emb = discord.Embed(
            title=f"🎶 Музыкальный плеер — {status}",
            description=f"**Сейчас:** {title}\n\n📃 Очередь ({len(self.queue)}):\n{qtxt}\n\n🔊 Громкость: **{vol}%**",
            color=discord.Color.green(),
        )
        if self.current and self.current[2]:
            emb.set_thumbnail(url=self.current[2])

        view = MusicControlView()
        try:
            # при каждом апдейте публикуем/обновлять старое сообщение не критично
            self.control_message = await self.text_channel.send(embed=emb, view=view)
        except Exception as e:
            logging.debug("update_panel error: %s", e)

    # helpers
    def pause(self):
        if self.vc and self.vc.is_playing():
            self.vc.pause()

    def resume(self):
        if self.vc and self.vc.is_paused():
            self.vc.resume()

    def stop(self):
        if self.vc and (self.vc.is_playing() or self.vc.is_paused()):
            self.vc.stop()

# ============================ VIEW (кнопки) ============================
class MusicControlView(View):
    def __init__(self):
        super().__init__(timeout=None)

    def _player(self, inter: Interaction) -> Optional[MusicPlayer]:
        if not inter.guild:
            return None
        return players.get(inter.guild.id)

    @discord.ui.button(label="⏯ Пауза/Продолжить", style=ButtonStyle.primary)
    async def toggle(self, inter: Interaction, _: Button):
        pl = self._player(inter)
        if not pl or not pl.vc or not pl.vc.is_connected():
            return await inter.response.send_message("❌ Бот не подключен.", ephemeral=True)
        if not inter.response.is_done():
            await inter.response.defer(ephemeral=True)
        if pl.vc.is_paused(): pl.resume()
        elif pl.vc.is_playing(): pl.pause()
        await pl.update_panel()

    @discord.ui.button(label="⏭ Пропустить", style=ButtonStyle.secondary)
    async def skip(self, inter: Interaction, _: Button):
        pl = self._player(inter)
        if not pl:
            return await inter.response.send_message("❌ Не играет.", ephemeral=True)
        if not inter.response.is_done():
            await inter.response.defer(ephemeral=True)
        pl.stop()
        await pl.update_panel()

    @discord.ui.button(label="🔉 Тише", style=ButtonStyle.secondary)
    async def vol_down(self, inter: Interaction, _: Button):
        pl = self._player(inter)
        if not pl:
            return await inter.response.send_message("❌ Не играет.", ephemeral=True)
        if not inter.response.is_done():
            await inter.response.defer(ephemeral=True)
        pl.volume = max(0.0, round(pl.volume - 0.1, 2))
        if pl.current_source and isinstance(pl.current_source, discord.PCMVolumeTransformer):
            pl.current_source.volume = pl.volume
        await pl.update_panel()

    @discord.ui.button(label="🔊 Громче", style=ButtonStyle.secondary)
    async def vol_up(self, inter: Interaction, _: Button):
        pl = self._player(inter)
        if not pl:
            return await inter.response.send_message("❌ Не играет.", ephemeral=True)
        if not inter.response.is_done():
            await inter.response.defer(ephemeral=True)
        pl.volume = min(2.0, round(pl.volume + 0.1, 2))
        if pl.current_source and isinstance(pl.current_source, discord.PCMVolumeTransformer):
            pl.current_source.volume = pl.volume
        await pl.update_panel()

    @discord.ui.button(label="🛑 Стоп", style=ButtonStyle.danger)
    async def stop(self, inter: Interaction, _: Button):
        pl = self._player(inter)
        if not pl:
            return await inter.response.send_message("Уже остановлено.", ephemeral=True)
        if not inter.response.is_done():
            await inter.response.defer(ephemeral=True)
        pl.stop()
        await pl._grace_and_leave()
        players.pop(inter.guild.id, None)
        await inter.followup.send("🛑 Остановлено.", ephemeral=True)

# ============================ DISCORD-БОТ ============================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree
players: Dict[int, MusicPlayer] = {}

# ---------- /play ----------
@tree.command(name="play", description="Воспроизвести музыку")
@app_commands.describe(query="Название трека или ссылка")
async def play_cmd(inter: Interaction, query: str):
    if not inter.response.is_done():
        try:
            await inter.response.defer(thinking=False)
        except discord.NotFound:
            return

    if not inter.user or not getattr(inter.user, "voice", None) or not inter.user.voice.channel:
        return await inter.followup.send("❌ Сначала зайди в голосовой канал.", ephemeral=True)

    ch = inter.user.voice.channel
    guild = inter.guild

    vc = discord.utils.get(bot.voice_clients, guild=guild)
    if not vc:
        vc = await ch.connect(self_deaf=False)
    elif vc.channel != ch:
        await vc.move_to(ch)

    # ждём до коннекта
    for _ in range(40):
        if vc.is_connected(): break
        await asyncio.sleep(0.1)
    if not vc.is_connected():
        return await inter.followup.send("❌ Не удалось подключиться к голосовому.", ephemeral=True)

    # yt-dlp: резолвим
    info = None
    if is_url(query):
        info = await ytdlp_extract(query, {"default_search": "auto"}, timeout=14.0)
    else:
        info = await ytdlp_extract(f"ytsearch1:{query}", timeout=10.0) or await ytdlp_extract(query, timeout=12.0)

    if info and "entries" in info:
        ent = (info.get("entries") or [])
        info = ent[0] if ent else None

    if not info:
        return await inter.followup.send("❌ Не удалось найти трек.", ephemeral=True)

    stream = info.get("url")
    title = info.get("title") or "Без названия"
    thumb = info.get("thumbnail")
    page = info.get("webpage_url") or (query if is_url(query) else None)

    if not stream and page:
        info2 = await ytdlp_extract(page, {"default_search": "auto"}, timeout=14.0)
        if info2 and "entries" in info2:
            ents = info2.get("entries") or []
            info2 = ents[0] if ents else None
        if info2:
            stream = info2.get("url")
            thumb = thumb or info2.get("thumbnail")
            title = title or info2.get("title")

    if not stream:
        return await inter.followup.send("❌ Не удалось получить поток для этого трека.", ephemeral=True)

    http_headers = info.get("http_headers") or {}
    http_headers.setdefault("User-Agent", http_headers.get("User-Agent", DEFAULT_UA))

    pl = players.get(guild.id)
    if not pl:
        pl = MusicPlayer(guild, vc, inter.channel, bot)  # type: ignore
        players[guild.id] = pl
    else:
        pl.vc = vc
        pl.text_channel = inter.channel  # type: ignore

    pl.queue.append((title, stream, thumb, page, http_headers))

    if not vc.is_playing() and not vc.is_paused():
        await pl.play_next()
    else:
        await pl.update_panel()
        await inter.followup.send(f"➕ **{title}** добавлен в очередь.", ephemeral=True)

# автодополнение для /play
@play_cmd.autocomplete("query")
async def _play_ac(inter: Interaction, current: str):
    try:
        return await smart_autocomplete(current)
    except Exception:
        return []

# ---------- /queue ----------
@tree.command(name="queue", description="Показать очередь")
async def queue_cmd(inter: Interaction):
    pl = players.get(inter.guild_id)
    if not pl or (not pl.queue and not pl.current):
        return await inter.response.send_message("Очередь пуста.", ephemeral=True)

    lines: List[str] = []
    if pl.current:
        lines.append(f"**Сейчас:** {pl.current[0]}")
    for i, t in enumerate(pl.queue[:20], 1):
        lines.append(f"{i}. {t[0]}")
    await inter.response.send_message("\n".join(lines)[:1900], ephemeral=True)

# ---------- /skip ----------
@tree.command(name="skip", description="Пропустить текущий трек")
async def skip_cmd(inter: Interaction):
    pl = players.get(inter.guild_id)
    if not pl or not pl.vc:
        return await inter.response.send_message("❌ Не играет.", ephemeral=True)
    pl.stop()
    await pl.update_panel()
    await inter.response.send_message("⏭ Пропущено.", ephemeral=True)

# ---------- /pause ----------
@tree.command(name="pause", description="Пауза/Продолжить")
async def pause_cmd(inter: Interaction):
    pl = players.get(inter.guild_id)
    if not pl or not pl.vc:
        return await inter.response.send_message("❌ Не играет.", ephemeral=True)
    if pl.vc.is_paused():
        pl.resume(); msg = "▶️ Продолжаю."
    elif pl.vc.is_playing():
        pl.pause(); msg = "⏸ Пауза."
    else:
        msg = "❌ Не играет."
    await inter.response.send_message(msg, ephemeral=True)

# ---------- /remove ----------
@tree.command(name="remove", description="Удалить трек из очереди по номеру (см. /queue)")
@app_commands.describe(index="Номер трека в очереди (как показывает /queue)")
async def remove_cmd(inter: Interaction, index: int):
    pl = players.get(inter.guild_id)
    if not pl or not pl.queue:
        return await inter.response.send_message("Очередь пуста.", ephemeral=True)
    if index < 1 or index > len(pl.queue):
        return await inter.response.send_message("Неверный номер.", ephemeral=True)
    title = pl.queue.pop(index - 1)[0]
    await pl.update_panel()
    await inter.response.send_message(f"🗑 Удалён: **{title}**", ephemeral=True)

# ---------- /stop ----------
@tree.command(name="stop", description="Остановить и выйти")
async def stop_cmd(inter: Interaction):
    pl = players.get(inter.guild_id)
    if not pl:
        return await inter.response.send_message("Уже остановлено.", ephemeral=True)
    pl.stop()
    await pl._grace_and_leave()
    players.pop(inter.guild_id, None)
    await inter.response.send_message("🛑 Остановлено.", ephemeral=True)



# Команда /say теперь только для админов
@bot.tree.command(name="say", description="Написать сообщение от бота")
@is_admin()
async def say(interaction: discord.Interaction):
    await interaction.response.send_modal(SayModal())

@say.error
async def say_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("❌ У вас нет доступа к этой команде.", ephemeral=True)

# ---------------- Приветствия (красивое оформление) ----------------
welcome_settings: dict[int, dict[str, int | str | bool]] = {}
# guild_id -> {"channel_id": int, "message": str, "use_banner": bool, "image_url": str}
DEFAULT_WELCOME = "👋 Привет, {user}! Добро пожаловать на сервер **{server}**! 🎉"

def _build_welcome_embed(guild: discord.Guild, text: str) -> discord.Embed:
    """
    Красивый embed:
      • set_author с названием и аватаркой сервера
      • set_thumbnail — аватар сервера «сбоку»
      • set_image — баннер сервера или кастомная картинка (если задана)
    """
    embed = discord.Embed(
        description=text,
        color=discord.Color.green()
    )

    # Автор — шапка эмбеда
    try:
        icon_url = guild.icon.url if guild.icon else None
    except Exception:
        icon_url = None
    embed.set_author(name=f"Добро пожаловать на {guild.name}!", icon_url=icon_url)

    # «Ава сбоку»
    if icon_url:
        try:
            embed.set_thumbnail(url=icon_url)
        except Exception:
            pass

    # Баннер или кастомная картинка
    st = welcome_settings.get(guild.id) or {}
    image_url: str | None = st.get("image_url") if isinstance(st.get("image_url"), str) else None
    use_banner = bool(st.get("use_banner"))  # если True и нет кастомной — возьмём баннер

    if image_url:
        try:
            embed.set_image(url=image_url)
        except Exception:
            pass
    elif use_banner and getattr(guild, "banner", None):
        try:
            embed.set_image(url=guild.banner.url)
        except Exception:
            pass

    embed.set_footer(text="Рады видеть тебя здесь!")
    embed.timestamp = discord.utils.utcnow()
    return embed


# /setup_welcome — выбрать канал (только админы по is_admin)
@bot.tree.command(name="setup_welcome", description="Выбрать канал для приветствия")
@app_commands.describe(channel="Канал, куда будет отправляться приветствие")
@is_admin()
async def setup_welcome(interaction: discord.Interaction, channel: discord.TextChannel):
    # проверим права бота
    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.send_messages and perms.embed_links):
        return await interaction.response.send_message(
            f"❌ У меня нет прав отправлять сообщения/вставлять embed в {channel.mention}.",
            ephemeral=True
        )

    st = welcome_settings.setdefault(interaction.guild.id, {})
    st["channel_id"] = channel.id
    st.setdefault("message", DEFAULT_WELCOME)
    st.setdefault("use_banner", True)   # по умолчанию используем баннер, если он есть
    st.setdefault("image_url", "")      # кастомная картинка не задана

    await interaction.response.send_message(
        f"✅ Канал для приветствий установлен: {channel.mention}",
        ephemeral=True
    )


# /set_welcome_message — текст приветствия
@bot.tree.command(name="set_welcome_message", description="Задать текст приветствия")
@app_commands.describe(message="Текст. Поддерживает {user} и {server}")
@is_admin()
async def set_welcome_message(interaction: discord.Interaction, message: str):
    if not message or len(message) > 1000:
        return await interaction.response.send_message("❌ Укажите непустой текст (до 1000 символов).", ephemeral=True)

    st = welcome_settings.setdefault(interaction.guild.id, {})
    st["message"] = message

    preview = message.replace("{user}", interaction.user.mention).replace("{server}", interaction.guild.name)
    await interaction.response.send_message(
        "✅ Текст приветствия сохранён.\n\n**Превью:**",
        ephemeral=True
    )
    # отправим отдельным embed'ом превью, чтобы не урезалось форматирование
    await interaction.followup.send(embed=_build_welcome_embed(interaction.guild, preview), ephemeral=True)


# /set_welcome_image — картинка внизу эмбеда: URL | "banner" | "none"
@bot.tree.command(name="set_welcome_image", description="Задать фон приветствия (картинка внизу эмбеда)")
@app_commands.describe(
    mode='Вариант: "banner" — использовать баннер сервера; "none" — без картинки; либо укажи URL изображения'
)
@is_admin()
async def set_welcome_image(interaction: discord.Interaction, mode: str):
    st = welcome_settings.setdefault(interaction.guild.id, {})
    mode = (mode or "").strip()

    if mode.lower() == "banner":
        st["image_url"] = ""
        st["use_banner"] = True
        msg = "🖼 Теперь используется баннер сервера (если задан в настройках сервера)."
    elif mode.lower() == "none":
        st["image_url"] = ""
        st["use_banner"] = False
        msg = "🚫 Картинка в приветствии отключена."
    else:
        # считаем, что это URL
        st["image_url"] = mode
        st["use_banner"] = False
        msg = f"🖼 Установлена кастомная картинка для приветствия."

    await interaction.response.send_message(f"✅ {msg}", ephemeral=True)


# /test_welcome — показать превью приветствия в текущем канале (только админам)
@bot.tree.command(name="test_welcome", description="Отправить превью приветствия в этот канал")
@is_admin()
async def test_welcome(interaction: discord.Interaction):
    st = welcome_settings.get(interaction.guild.id) or {}
    ch_id = st.get("channel_id")
    msg = st.get("message") or DEFAULT_WELCOME

    preview_text = str(msg).replace("{user}", interaction.user.mention).replace("{server}", interaction.guild.name)
    embed = _build_welcome_embed(interaction.guild, preview_text)

    # проверим права для текущего канала (мы шлём превью сюда)
    perms = interaction.channel.permissions_for(interaction.guild.me)
    if not (perms.send_messages and perms.embed_links):
        return await interaction.response.send_message("❌ Нет прав отправлять embed в этот канал.", ephemeral=True)

    await interaction.response.send_message("✅ Превью отправлено ниже.", ephemeral=True)
    await interaction.channel.send(embed=embed)
    if ch_id:
        await interaction.followup.send(f"ℹ️ Рабочий канал приветствий: <#{ch_id}>", ephemeral=True)


# Слушатель входа — отправляем красивый embed
@bot.listen("on_member_join")
async def _welcome_on_join(member: discord.Member):
    st = welcome_settings.get(member.guild.id)
    if not st:
        return

    channel_id = st.get("channel_id")
    if not channel_id:
        return

    channel = member.guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    perms = channel.permissions_for(member.guild.me)
    if not (perms.send_messages and perms.embed_links):
        return

    raw = st.get("message") or DEFAULT_WELCOME
    text = raw.replace("{user}", member.mention).replace("{server}", member.guild.name)

    embed = _build_welcome_embed(member.guild, text)

    try:
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass



# --- Хранение ролей поддержки ---
support_roles: dict[int, list[int]] = {}  # guild_id -> [role_id, role_id, ...]

# --- Команда для установки ролей поддержки ---
@bot.tree.command(name="set_support_roles", description="Задать роли, которые могут отвечать в тикетах")
@app_commands.describe(role_names="Названия ролей через запятую")
@is_admin()
async def set_support_roles(interaction: discord.Interaction, role_names: str):
    names = [r.strip() for r in role_names.split(",") if r.strip()]
    roles: list[discord.Role] = []
    for name in names:
        role = discord.utils.get(interaction.guild.roles, name=name)
        if role:
            roles.append(role)

    if not roles:
        return await interaction.response.send_message("❌ Роли не найдены.", ephemeral=True)

    support_roles[interaction.guild.id] = [r.id for r in roles]
    await interaction.response.send_message(
        f"✅ Роли поддержки установлены: {', '.join(r.mention for r in roles)}",
        ephemeral=True
    )



# --- Тикеты (панель только для выбранных ролей, тикет может создать любой) ---

class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎫 Создать тикет", style=discord.ButtonStyle.primary, custom_id="create_ticket")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Любой пользователь может создать тикет (убрали проверку на администратора)
        guild = interaction.guild

        # Ищем/создаём категорию
        category = discord.utils.get(guild.categories, name="Tickets")
        if not category:
            try:
                category = await guild.create_category("Tickets", reason="Категория для тикетов")
            except discord.Forbidden:
                return await interaction.response.send_message("❌ У меня нет прав создать категорию.", ephemeral=True)

        # внутри TicketView.create_ticket
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                          read_message_history=True),
        }

        # роли поддержки из словаря
        for rid in support_roles.get(guild.id, []):
            role = guild.get_role(rid)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )

        support_role = discord.utils.get(guild.roles, name="Support")
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )

        # Создаём текстовый канал
        safe_name = f"тикет-{interaction.user.name}".lower().replace(" ", "-")
        try:
            ticket_channel = await guild.create_text_channel(
                name=safe_name[:90],
                category=category,
                overwrites=overwrites,
                reason=f"Тикет от {interaction.user} ({interaction.user.id})"
            )
        except discord.HTTPException:
            return await interaction.response.send_message("❌ Не удалось создать канал тикета.", ephemeral=True)

        # Сообщение внутри тикета
        open_embed = discord.Embed(
            title="🎟️ Тикет создан",
            description=(
                f"{interaction.user.mention}, спасибо за обращение!\n"
                "Опишите, пожалуйста, вашу проблему или вопрос максимально подробно. "
                "Наши модераторы скоро подключатся. 🙌"
            ),
            color=discord.Color.green()
        )
        if guild.icon:
            try:
                open_embed.set_thumbnail(url=guild.icon.url)
            except Exception:
                pass

        await ticket_channel.send(embed=open_embed, view=CloseTicketView())

        await interaction.response.send_message(
            f"✅ Тикет создан: {ticket_channel.mention}", ephemeral=True
        )


class CloseTicketView(discord.ui.View):
    @discord.ui.button(label="❌ Закрыть тикет", style=discord.ButtonStyle.danger, custom_id="close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Закрыть может владелец тикета или админ (из выбранных ролей) или владелец сервера
        is_owner = interaction.user in interaction.channel.members
        is_server_owner = interaction.user.id == interaction.guild.owner_id

        # проверка через твою систему админ-ролей
        user_roles = [r.id for r in interaction.user.roles]
        guild_admins = set(admin_roles.get(interaction.guild.id, []))
        is_admin_role = bool(guild_admins.intersection(user_roles))

        if not (is_owner or is_admin_role or is_server_owner or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("❌ У вас нет прав закрывать этот тикет.", ephemeral=True)
            return

        try:
            await interaction.response.send_message("🔒 Тикет будет удалён через 5 секунд.", ephemeral=True)
        except discord.InteractionResponded:
            pass
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Закрыт пользователем {interaction.user}")
        except discord.Forbidden:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ Нет прав удалить канал.", ephemeral=True)


# --- Команда /тикеты: панель может отправить только выбранные роли (is_admin) ---
@bot.tree.command(name="тикеты", description="Создать сообщение с кнопкой тикета")
@is_admin()
async def тикеты(interaction: discord.Interaction):
    # проверим права бота на отправку/встраивание
    perms = interaction.channel.permissions_for(interaction.guild.me)
    if not (perms.send_messages and perms.embed_links):
        return await interaction.response.send_message("❌ У меня нет прав отправлять сообщения/вставлять embed здесь.", ephemeral=True)

    # Красивый embed с иконкой сервера
    guild = interaction.guild
    embed = discord.Embed(
        title=f"📩 Поддержка — {guild.name}",
        description=(
            "Нужна помощь? Мы рядом! ✨\n\n"
            "**Нажмите кнопку ниже, чтобы создать персональный тикет.**\n"
            "В вашем канале сможете общаться с модераторами один на один. "
            "Опишите проблему подробно — так мы поможем быстрее. 💬"
        ),
        color=discord.Color.blurple()
    )
    if guild.icon:
        try:
            embed.set_thumbnail(url=guild.icon.url)
        except Exception:
            pass
    embed.set_footer(text="Тикеты видны только вам и команде поддержки.")

    await interaction.channel.send(embed=embed, view=TicketView())
    await interaction.response.send_message("✅ Панель тикетов отправлена.", ephemeral=True)


# — обработка ошибок доступа для /тикеты —
@тикеты.error
async def тикеты_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        if interaction.response.is_done():
            await interaction.followup.send("❌ У вас нет доступа к этой команде.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ У вас нет доступа к этой команде.", ephemeral=True)





# --- Модальные окна ---
class LimitModal(discord.ui.Modal, title="Изменить лимит"):
    limit = discord.ui.TextInput(label="Новый лимит (0 = без лимита)", required=True, max_length=2)

    def __init__(self, voice_channel: discord.VoiceChannel):
        super().__init__()
        self.voice_channel = voice_channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_limit = int(self.limit.value)
            if not (0 <= new_limit <= 99):
                await interaction.response.send_message("❌ Лимит должен быть от 0 до 99.", ephemeral=True)
                return
            await self.voice_channel.edit(user_limit=new_limit)
            await interaction.response.send_message(f"✅ Лимит изменён на {new_limit}.", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("❌ Некорректное число.", ephemeral=True)


class RenameModal(discord.ui.Modal, title="Переименовать канал"):
    new_name = discord.ui.TextInput(label="Новое имя канала", required=True, max_length=100)

    def __init__(self, voice_channel: discord.VoiceChannel):
        super().__init__()
        self.voice_channel = voice_channel

    async def on_submit(self, interaction: discord.Interaction):
        await self.voice_channel.edit(name=self.new_name.value)
        await interaction.response.send_message(f"✅ Канал переименован в {self.new_name.value}.", ephemeral=True)


class InviteModal(discord.ui.Modal, title="Пригласить пользователей"):
    users_input = discord.ui.TextInput(
        label="Упоминания или ID пользователей",
        placeholder="@User1 @User2 123456789012345678",
        required=True,
        max_length=500
    )

    MENTION_RE = re.compile(r"<@!?(\d+)>")

    def __init__(self, voice_channel: discord.VoiceChannel, owner: discord.User):
        super().__init__()
        self.voice_channel = voice_channel
        self.owner = owner

    def _parse_members(self, guild: discord.Guild, text: str) -> list[discord.Member]:
        ids: set[int] = set()

        # 1) все упоминания вида <@123> и <@!123>
        for m in self.MENTION_RE.findall(text):
            try:
                ids.add(int(m))
            except ValueError:
                pass

        # 2) все «голые» ID (цифры)
        for token in re.split(r"[,\s]+", text.strip()):
            if token.isdigit():
                try:
                    ids.add(int(token))
                except ValueError:
                    pass

        # собираем членов по ID
        members: list[discord.Member] = []
        for uid in ids:
            m = guild.get_member(uid)
            if m:
                members.append(m)
        return members

    async def on_submit(self, interaction: discord.Interaction):
        # проверка владельца канала
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("❌ Это не ваш канал!", ephemeral=True)
            return

        # сразу деферйм, чтобы можно было безопасно делать followup
        await interaction.response.defer(ephemeral=True, thinking=False)

        guild = interaction.guild
        members = self._parse_members(guild, self.users_input.value)

        if not members:
            await interaction.followup.send("❌ Пользователи не найдены. Укажите @упоминания или ID.", ephemeral=True)
            return

        # Выдаём права на голосовой канал и отправляем ЛС
        ok: list[str] = []
        failed_dm: list[str] = []
        failed_perm: list[str] = []

        for m in members:
            # 1) права на канал (на случай закрытого канала)
            try:
                await self.voice_channel.set_permissions(
                    m,
                    view_channel=True,
                    connect=True,
                    speak=True
                )
            except discord.Forbidden:
                failed_perm.append(m.mention)
                continue
            except discord.HTTPException:
                failed_perm.append(m.mention)
                continue

            # 2) отправка ЛС с «ссылкой»
            try:
                jump_url = f"https://discord.com/channels/{guild.id}/{self.voice_channel.id}"
                dm_text = (
                    f"👋 Привет! {interaction.user.mention} приглашает тебя в голосовой канал "
                    f"**{self.voice_channel.name}** на сервере **{guild.name}**.\n"
                    f"Перейти: {jump_url}"
                )
                await m.send(dm_text)
                ok.append(m.mention)
            except discord.Forbidden:
                failed_dm.append(m.mention)
            except discord.HTTPException:
                failed_dm.append(m.mention)

        # Итоговый отчёт
        parts = []
        if ok:
            parts.append(f"✅ Права выданы и приглашение отправлено: {', '.join(ok)}")
        if failed_perm:
            parts.append(f"⚠️ Не удалось выдать права: {', '.join(failed_perm)}")
        if failed_dm:
            parts.append(f"📭 ЛС закрыты/не доставлено: {', '.join(failed_dm)}")

        if not parts:
            parts.append("❌ Никого пригласить не удалось.")

        await interaction.followup.send("\n".join(parts), ephemeral=True)



# --- Панель управления каналом ---
class TempVCManageView(discord.ui.View):
    def __init__(self, voice_channel: discord.VoiceChannel, user: discord.User):
        super().__init__(timeout=300)
        self.voice_channel = voice_channel
        self.user = user

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ Это не ваш голосовой канал!", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Закрыть канал", style=discord.ButtonStyle.danger, emoji="🔒")
    async def close_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Канал будет удалён через 5 секунд...", ephemeral=True)
        await asyncio.sleep(5)
        await self.voice_channel.delete()
        user_temp_vcs.pop((interaction.guild.id, self.user.id), None)

    @discord.ui.button(label="Открыть канал", style=discord.ButtonStyle.success, emoji="🔓")
    async def open_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.voice_channel.set_permissions(interaction.guild.default_role, connect=True)
        await interaction.response.send_message("✅ Канал открыт для всех.", ephemeral=True)

    @discord.ui.button(label="Закрыть для всех", style=discord.ButtonStyle.secondary, emoji="🚫")
    async def lock_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.voice_channel.set_permissions(interaction.guild.default_role, connect=False)
        await interaction.response.send_message("🔒 Канал закрыт для всех.", ephemeral=True)

    @discord.ui.button(label="Изменить лимит", style=discord.ButtonStyle.primary, emoji="📊")
    async def change_limit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LimitModal(self.voice_channel))

    @discord.ui.button(label="Переименовать", style=discord.ButtonStyle.secondary, emoji="✏️")
    async def rename_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RenameModal(self.voice_channel))

    @discord.ui.button(label="Пригласить", style=discord.ButtonStyle.success, emoji="📩")
    async def invite_members(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(InviteModal(self.voice_channel, self.user))



class ControlMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Управлять моим каналом", style=discord.ButtonStyle.primary, custom_id="manage_my_vc")
    async def manage_my_vc(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        guild_id = interaction.guild.id
        if (guild_id, user_id) not in user_temp_vcs:
            await interaction.response.send_message("❌ У вас нет временного голосового канала.", ephemeral=True)
            return
        vc_id = user_temp_vcs[(guild_id, user_id)]
        voice_channel = interaction.guild.get_channel(vc_id)
        if not voice_channel:
            await interaction.response.send_message("❌ Ваш временный канал не найден.", ephemeral=True)
            return

        view = TempVCManageView(voice_channel, interaction.user)
        await interaction.response.send_message(f"🎙 Меню управления каналом **{voice_channel.name}**", view=view,
                                                ephemeral=True)


# --- Панель управления войсами (только для админов по is_admin) ---
@bot.tree.command(name="панель_войса", description="Отправить меню управления временными войсами")
@app_commands.describe(channel="Канал, куда отправить меню")
@is_admin()
async def панель_войса(interaction: discord.Interaction, channel: discord.TextChannel):
    # проверим права бота в целевом канале
    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.send_messages and perms.embed_links):
        return await interaction.response.send_message(
            f"❌ У меня нет прав отправлять сообщения/вставлять embed в {channel.mention}.",
            ephemeral=True
        )

    # не дублируем панель (ищем похожий embed от бота)
    try:
        async for msg in channel.history(limit=20):
            if msg.author == bot.user and msg.embeds and msg.embeds[0].title:
                if "🎙 Управление временными голосовыми каналами" in msg.embeds[0].title:
                    return await interaction.response.send_message(
                        "⚠️ Панель уже существует в этом канале.", ephemeral=True
                    )
    except discord.Forbidden:
        return await interaction.response.send_message(
            "❌ Нет доступа к истории сообщений этого канала.", ephemeral=True
        )

    embed = discord.Embed(
        title="🎙 Управление временными голосовыми каналами",
        description=(
            "Привет! 👋 Здесь вы можете **полностью контролировать свой личный голосовой канал**.\n\n"
            "Возможности панели:\n"
            "🔹 Изменить лимит участников\n"
            "🔹 Переименовать канал\n"
            "🔹 Открыть или закрыть доступ для всех\n"
            "🔹 Пригласить выбранных пользователей\n"
            "🔹 Удалить канал вручную\n\n"
            "Нажмите кнопку ниже, чтобы открыть меню управления. ✨"
        ),
        color=discord.Color.blue()
    )
    embed.set_footer(text="Доступно только владельцам временного голосового канала.")

    await channel.send(embed=embed, view=ControlMenuView())
    await interaction.response.send_message(
        f"✅ Панель управления временными войсами успешно отправлена в {channel.mention}!",
        ephemeral=True
    )


# — обработка ошибок доступа —
@панель_войса.error
async def панель_войса_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        if interaction.response.is_done():
            await interaction.followup.send("❌ У вас нет доступа к этой команде.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ У вас нет доступа к этой команде.", ephemeral=True)


# --- Просмотр настроек временных войсов (только для админов) ---
@bot.tree.command(name="voice_settings", description="Показать текущие настройки голосовых каналов")
@is_admin()
async def voice_settings(interaction: discord.Interaction):
    guild_id = interaction.guild.id

    if guild_id not in server_settings:
        await interaction.response.send_message(
            "❌ Настройки ещё не заданы. Используйте /setup_voice.",
            ephemeral=True
        )
        return

    settings = server_settings[guild_id]
    trigger_channel = interaction.guild.get_channel(settings.get("trigger_channel_id"))
    category = interaction.guild.get_channel(settings.get("temp_category_id"))

    embed = discord.Embed(
        title="⚙️ Настройки временных голосовых",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="Триггерный канал",
        value=trigger_channel.mention if trigger_channel else "❌ Не найден",
        inline=False
    )
    embed.add_field(
        name="Категория для временных каналов",
        value=category.name if category else "❌ Не найдена",
        inline=False
    )
    embed.set_footer(text="Для изменения используйте /setup_voice")

    await interaction.response.send_message(embed=embed, ephemeral=True)


# --- Обработка ошибок доступа ---
@voice_settings.error
async def voice_settings_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("❌ У вас нет доступа к этой команде.", ephemeral=True)


# --- Настройка временных войсов: только для админов по is_admin() ---
@bot.tree.command(name="setup_voice", description="Настроить канал для создания временных голосовых")
@app_commands.describe(
    trigger_channel="Канал, при входе в который создается временный голосовой",
    category="Категория для временных голосовых (необязательно)"
)
@is_admin()
async def setup_voice(
    interaction: discord.Interaction,
    trigger_channel: discord.VoiceChannel,
    category: discord.CategoryChannel | None = None
):
    guild = interaction.guild
    guild_id = guild.id

    # Если категорию не указали — создаём/находим дефолтную
    if category is None:
        category = discord.utils.get(guild.categories, name="Temporary Voice")
        if category is None:
            try:
                category = await guild.create_category("Temporary Voice", reason="Категория для временных войсов")
            except discord.Forbidden:
                return await interaction.response.send_message(
                    "❌ У меня нет прав создать категорию. Проверьте права.", ephemeral=True
                )

    server_settings[guild_id] = {
        "trigger_channel_id": trigger_channel.id,
        "temp_category_id": category.id,
    }

    await interaction.response.send_message(
        f"✅ Настройки сохранены!\n"
        f"• Триггерный канал: {trigger_channel.mention}\n"
        f"• Категория: {category.name}",
        ephemeral=True
    )


# --- Событие создания/удаления временного канала ---
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    guild = member.guild
    guild_id = guild.id
    user_id = member.id

    settings = server_settings.get(guild_id)
    if not settings:
        return

    trigger_id = settings.get("trigger_channel_id")
    category_id = settings.get("temp_category_id")
    if not trigger_id or not category_id:
        return

    # Пользователь вошёл в триггерный канал → создать личный VC и переместить
    if after.channel and after.channel.id == trigger_id:
        category = guild.get_channel(category_id)
        if isinstance(category, discord.CategoryChannel):
            try:
                temp_vc = await category.create_voice_channel(f"{member.display_name}'s VC")
                user_temp_vcs[(guild_id, user_id)] = temp_vc.id
                await member.move_to(temp_vc)
            except discord.Forbidden:
                pass  # не хватает прав — молча игнорируем
            except discord.HTTPException:
                pass

    # Пользователь покинул канал → если это его временный VC и он пуст — удалить
    if before.channel:
        key = (guild_id, user_id)
        vc_id = user_temp_vcs.get(key)
        if vc_id and before.channel.id == vc_id:
            try:
                if len(before.channel.members) == 0:
                    user_temp_vcs.pop(key, None)
                    await before.channel.delete(reason="Пустой временный канал")
            except (discord.Forbidden, discord.HTTPException):
                pass


# --- SETLOG только для админов ---
@bot.tree.command(name="setlog", description="Установить канал для логов")
@app_commands.describe(channel="Канал для логов")
@is_admin()
async def setlog(interaction: discord.Interaction, channel: discord.TextChannel):
    log_channels[interaction.guild.id] = channel.id
    await interaction.response.send_message(
        f"✅ Канал для логов установлен на {channel.mention}", ephemeral=True
    )


# --- Функция логирования ---
async def log(guild: discord.Guild, message: str):
    channel_id = log_channels.get(guild.id)
    if not channel_id:
        return  # лог не настроен для этого сервера
    channel = guild.get_channel(channel_id)
    if channel:
        try:
            await channel.send(message)
        except discord.Forbidden:
            print(f"⚠️ Нет доступа для отправки в канал логов {channel.id} на сервере {guild.id}")

# Пример логов для разных событий

@bot.event
async def on_member_join(member):
    await log(member.guild, f"➡️ Пользователь {member.mention} присоединился к серверу.")

@bot.event
async def on_member_remove(member):
    await log(member.guild, f"⬅️ Пользователь {member.name}#{member.discriminator} покинул сервер.")

@bot.event
async def on_message_delete(message):
    if message.author.bot:
        return

    content = message.content if message.content else ""
    attachments = ", ".join(attachment.url for attachment in message.attachments)

    log_text = ""
    if content:
        log_text += content
    if attachments:
        if log_text:
            log_text += "\n"
        log_text += f"📎 Вложения: {attachments}"

    if not log_text:
        log_text = "[Нет текста или вложений]"

    await log(
        message.guild,
        f"🗑️ Сообщение пользователя {message.author.mention} удалено в канале {message.channel.mention}:\n> {log_text}"
    )

@bot.event
async def on_message_edit(before, after):
    if before.author.bot:
        return
    before_content = before.content if before.content else "[Нет текста]"
    after_content = after.content if after.content else "[Нет текста]"
    if before_content == after_content:
        return  # Если текст не изменился, логировать не нужно
    await log(
        before.guild,
        f"✏️ Сообщение от {before.author.mention} отредактировано в {before.channel.mention}:\n"
        f"Было: > {before_content}\n"
        f"Стало: > {after_content}"
    )


@bot.event
async def on_member_update(before, after):
    if before.nick != after.nick:
        await log(before.guild, f"📝 У пользователя {before.mention} изменился ник с '{before.nick}' на '{after.nick}'")

@bot.event
async def on_guild_role_update(before, after):
    await log(before.guild, f"⚙️ Роль {before.name} была обновлена.")

@bot.event
async def on_member_ban(guild, user):
    await log(guild, f"⛔ Пользователь {user.name}#{user.discriminator} был забанен.")

@bot.event
async def on_member_unban(guild, user):
    await log(guild, f"✅ Пользователь {user.name}#{user.discriminator} был разбанен.")

@bot.event
async def on_command_error(ctx, error):
    await log(ctx.guild, f"❗ Ошибка в команде {ctx.command}: {error}")





async def setup_muted_role(guild: discord.Guild):
    """Создаёт или получает роль Muted и настраивает права"""
    role = discord.utils.get(guild.roles, name="Muted")
    if role is None:
        role = await guild.create_role(name="Muted", reason="Роль для мута")

        # Применяем права ко всем существующим каналам
        for channel in guild.channels:
            await channel.set_permissions(
                role,
                send_messages=False,
                speak=False,
                add_reactions=False,
                stream=False,
                connect=False  # Запрещаем подключение к голосовым
            )

    return role


async def mute_user(member: discord.Member, guild: discord.Guild, context_channel: discord.TextChannel):
    """Мьютит пользователя"""
    role = await setup_muted_role(guild)

    if role in member.roles:
        return  # Уже замьючен

    await member.add_roles(role, reason="Автоматический мут за спам")

    try:
        await context_channel.send(f"🔇 {member.mention} был автоматически замьючен на 2 часа за спам.")
    except Exception as e:
        print(f"Ошибка при отправке сообщения в канал: {e}")

    # Ждём 2 часа и снимаем мут
    await asyncio.sleep(MUTE_DURATION)

    if role in member.roles:
        await member.remove_roles(role, reason="Истёк срок мута")
        try:
            await member.send(f"✅ Ваш мут в **{guild.name}** снят. Пожалуйста, не спамьте снова.")
        except discord.Forbidden:
            pass


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    now = time.time()
    user_id = message.author.id

    # Получаем историю сообщений пользователя
    history = user_message_history.get(user_id, [])
    # Очищаем старые сообщения
    history = [timestamp for timestamp in history if now - timestamp < SPAM_TIME_WINDOW]
    history.append(now)
    user_message_history[user_id] = history

    # Если пользователь спамит
    if len(history) >= SPAM_THRESHOLD:
        # Проверяем, не замьючен ли уже пользователь
        if not any(role.name == "Muted" for role in message.author.roles):
            await mute_user(message.author, message.guild, message.channel)
            user_message_history[user_id] = []  # сбросим историю после мута

    await bot.process_commands(message)




# --- CLEAR только для админов ---
@bot.tree.command(name="clear", description="Удаляет сообщения в канале (доступно только админам)")
@app_commands.describe(amount="Количество сообщений (1–100)")
@is_admin()
async def slash_clear(interaction: discord.Interaction, amount: int):
    if amount < 1 or amount > 100:
        await interaction.response.send_message("❌ Укажите число от 1 до 100.", ephemeral=True)
        return

    try:
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.response.send_message(f"✅ Удалено {len(deleted)} сообщений.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ У меня нет прав на удаление сообщений.", ephemeral=True)
    except discord.HTTPException as e:
        await interaction.response.send_message(f"❌ Ошибка Discord API: {e}", ephemeral=True)


# --- Обработка ошибок ---
@slash_clear.error
async def clear_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("❌ У вас нет доступа к этой команде.", ephemeral=True)


# -------------- STARTUP & PERSISTENT VIEW --------------
@bot.event
async def on_ready():
    logging.info("✅ Бот %s запущен!", bot.user)
    activity = discord.Game(name="  |  /help ❤")
    await bot.change_presence(status=discord.Status.online, activity=activity)
    try:
        # регистрируем persistent view по custom_id (без привязки к guild_id)
        bot.add_view(MusicControlView())
    except Exception as e:
        logging.error("add_view error: %s", e)

    # синхронизация команд
    try:
        synced = await tree.sync()
        names = ", ".join(sorted([c.name for c in synced]))
        logging.info("🔄 Синхронизировано %d глобальных команд: %s", len(synced), names)
    except Exception as e:
        logging.error("Ошибка sync: %s", e)


# -------------- RUN --------------
if __name__ == "__main__":
    import os

    # Читаем из переменной окружения DISCORD_TOKEN
    TOKEN = os.getenv("DISCORD_TOKEN")

    if not TOKEN:
        # fallback — можно вставить прямо в код, если не хочешь через env
        TOKEN = ""

    bot.run(TOKEN)
