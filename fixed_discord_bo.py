import asyncio
import copy
import logging
import os
import re
import time
from typing import Optional, List, Tuple, Dict
from functools import partial

import discord
import yt_dlp
from discord import app_commands, Interaction
from discord.ext import commands
from discord.ui import View, Button, Select, Modal, TextInput
from discord import ButtonStyle

# --- Инициализация бота ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# --- Логирование ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# --- Глобальные переменные ---
user_message_history = {}
SPAM_THRESHOLD = 5
SPAM_TIME_WINDOW = 4
MUTE_DURATION = 2 * 60 * 60

user_temp_vcs = {}
server_settings = {}
log_channels = {}
balances = {}
shop_items = []
admin_roles = {}
support_roles = {}
welcome_settings = {}

MOD_ROLE_RANKS: Dict[int, Dict[int, int]] = {}
LOCK_SNAPSHOTS: Dict[int, Dict[int, Dict[str, dict]]] = {}

# --- Музыкальные настройки ---
music_players: Dict[int, "MusicPlayer"] = {}

YTDLP_STREAM_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "source_address": "0.0.0.0",
    "geo_bypass": True,
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn"
}


DEFAULT_WELCOME = "👋 Привет, {user}! Добро пожаловать на сервер **{server}**! 🎉"

# ============================================================================
# УТИЛИТЫ
# ============================================================================

def is_admin():
    """Проверка на админа"""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == interaction.guild.owner_id:
            return True
        
        guild_id = interaction.guild.id
        if guild_id in admin_roles:
            user_roles = [role.id for role in interaction.user.roles]
            for r in admin_roles[guild_id]:
                if r in user_roles:
                    return True
        
        return interaction.user.guild_permissions.administrator
    
    return app_commands.check(predicate)


def _get_member_rank(member: discord.Member) -> int:
    """Получить ранг модератора"""
    if member.id == member.guild.owner_id:
        return 99
    
    ranks = MOD_ROLE_RANKS.get(member.guild.id, {})
    max_rank = 0
    
    for role in member.roles:
        r = ranks.get(role.id, 0)
        if r > max_rank:
            max_rank = r
    
    return max_rank


def requires_rank(min_rank: int):
    """Декоратор для проверки ранга модератора"""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        if interaction.user.id == interaction.guild.owner_id:
            return True
        return _get_member_rank(interaction.user) >= min_rank
    
    return app_commands.check(predicate)


async def log(guild: discord.Guild, message: str):
    """Логирование в канал"""
    channel_id = log_channels.get(guild.id)
    if not channel_id:
        return
    
    channel = guild.get_channel(channel_id)
    if channel and isinstance(channel, discord.TextChannel):
        try:
            await channel.send(message)
        except discord.Forbidden:
            logging.warning(f"Нет прав для логов в канале {channel_id}")


def _build_welcome_embed(guild: discord.Guild, text: str) -> discord.Embed:
    """Красивый embed для приветствия"""
    embed = discord.Embed(
        description=text,
        color=discord.Color.green()
    )
    
    try:
        icon_url = guild.icon.url if guild.icon else None
    except Exception:
        icon_url = None
    
    embed.set_author(name=f"Добро пожаловать на {guild.name}!", icon_url=icon_url)
    
    if icon_url:
        try:
            embed.set_thumbnail(url=icon_url)
        except Exception:
            pass
    
    st = welcome_settings.get(guild.id) or {}
    image_url = st.get("image_url") if isinstance(st.get("image_url"), str) else None
    use_banner = bool(st.get("use_banner"))
    
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


# ============================================================================
# МУЗЫКАЛЬНЫЙ ПЛЕЕР
# ============================================================================

class MusicPlayer:
    """Музыкальный плеер для сервера"""
    
    def __init__(self, guild: discord.Guild, vc: discord.VoiceClient,
                 text_channel: discord.abc.Messageable):
        self.guild = guild
        self.vc = vc
        self.text_channel = text_channel
        self.volume = 0.5
        self.current_source: Optional[discord.PCMVolumeTransformer] = None
        self.queue: List[Tuple[str, str, Optional[str]]] = []
        self.current_track: Optional[Tuple[str, str, Optional[str]]] = None
        self.control_message: Optional[discord.Message] = None
        self._play_lock = asyncio.Lock()

    async def add_track(self, query: str) -> Tuple[bool, str]:
        """Добавить трек в очередь с корректным потоком для FFmpeg"""
        try:
            search_query = query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"

            with yt_dlp.YoutubeDL(YTDLP_STREAM_OPTS) as ydl:
                info = ydl.extract_info(search_query, download=False)

                # Если это поиск, берем первый результат
                if "entries" in info:
                    info = info["entries"][0]

                # Получаем URL для потока без видео
                formats = info.get("formats", [info])
                audio_format = next(
                    (f for f in formats if f.get("acodec") != "none" and f.get("vcodec") == "none"),
                    None
                )
                if not audio_format or not audio_format.get("url"):
                    return False, "❌ Не удалось получить поток для FFmpeg"

                title = info.get("title", "Unknown")
                stream_url = audio_format["url"]
                thumbnail = info.get("thumbnail")

                self.queue.append((title, stream_url, thumbnail))
                return True, f"➕ **{title}** добавлен в очередь"

        except Exception as e:
            logging.error(f"Ошибка добавления трека: {e}")
            return False, f"❌ Ошибка: {str(e)}"

    async def play_next(self):
        """Воспроизвести следующий трек"""
        async with self._play_lock:
            if not self.queue:
                await self.stop_and_cleanup()
                return
            
            title, stream_url, thumbnail = self.queue.pop(0)
            self.current_track = (title, stream_url, thumbnail)

            try:
                ffmpeg_options = {
                    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                    "options": "-vn -f s16le -ar 48000 -ac 2"
                }

                # создаём источник
                source = discord.FFmpegPCMAudio(
                    stream_url,
                    executable="/usr/bin/ffmpeg",
                    **ffmpeg_options
                )

                # громкость
                self.current_source = discord.PCMVolumeTransformer(source, volume=self.volume)

                # колбэк после завершения трека
                def after_playing(error):
                    if error:
                        logging.error(f"Ошибка воспроизведения: {error}")
                    coro = self.play_next()
                    asyncio.run_coroutine_threadsafe(coro, bot.loop)

                # запускаем трек
                self.vc.play(self.current_source, after=after_playing)
                await self.update_control_message()

            except Exception as e:
                logging.error(f"FFmpeg ошибка: {e}")
                await self.play_next()

    async def stop_and_cleanup(self):
        """Остановить плеер и очистить ресурсы"""
        try:
            if self.vc and self.vc.is_connected():
                await self.vc.disconnect(force=True)
        except Exception as e:
            logging.error(f"Ошибка отключения: {e}")
        
        if self.control_message:
            try:
                await self.control_message.edit(view=None)
                await self.control_message.delete()
            except Exception:
                pass
        
        self.current_track = None
        self.current_source = None
        self.queue.clear()
    
    async def update_control_message(self):
        """Обновить панель управления"""
        if self.vc.is_playing():
            status = "▶️ Воспроизведение"
        elif self.vc.is_paused():
            status = "⏸ Пауза"
        else:
            status = "❌ Не играет"
        
        current_title = self.current_track[0] if self.current_track else "Нет трека"
        
        queue_lines = []
        for i, track in enumerate(self.queue[:10], 1):
            queue_lines.append(f"{i}. {track[0]}")
        queue_text = "\n".join(queue_lines) or "Очередь пуста"
        
        vol_pct = int(self.volume * 100)
        description = (
            f"**Сейчас играет:** {current_title}\n\n"
            f"📃 **Очередь ({len(self.queue)}):**\n{queue_text}\n\n"
            f"🔊 **Громкость:** {vol_pct}%"
        )
        
        embed = discord.Embed(
            title=f"🎶 Музыкальный плеер — {status}",
            description=description,
            color=discord.Color.green()
        )
        
        if self.current_track and self.current_track[2]:
            embed.set_thumbnail(url=self.current_track[2])
        
        view = MusicControlView()
        
        if self.control_message:
            try:
                await self.control_message.edit(embed=embed, view=view)
                return
            except Exception:
                self.control_message = None
        
        try:
            self.control_message = await self.text_channel.send(embed=embed, view=view)
        except Exception as e:
            logging.error(f"Ошибка отправки панели: {e}")
    
    def pause(self):
        if self.vc and self.vc.is_playing():
            self.vc.pause()
    
    def resume(self):
        if self.vc and self.vc.is_paused():
            self.vc.resume()
    
    def stop(self):
        if self.vc and (self.vc.is_playing() or self.vc.is_paused()):
            self.vc.stop()


class MusicControlView(View):
    """Панель управления музыкой"""
    
    def __init__(self):
        super().__init__(timeout=None)
    
    @staticmethod
    def _get_player(interaction: Interaction) -> Optional[MusicPlayer]:
        if not interaction.guild:
            return None
        return music_players.get(interaction.guild.id)
    
    @discord.ui.button(label="⏯ Вкл/Пауза", style=ButtonStyle.primary, custom_id="mp_toggle")
    async def pause_resume(self, interaction: Interaction, _: Button):
        player = self._get_player(interaction)
        if not player or not player.vc or not player.vc.is_connected():
            await interaction.response.send_message("❌ Бот не подключен", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        if player.vc.is_paused():
            player.resume()
        elif player.vc.is_playing():
            player.pause()
        
        await player.update_control_message()
    
    @discord.ui.button(label="⏭ Пропустить", style=ButtonStyle.secondary, custom_id="mp_skip")
    async def skip(self, interaction: Interaction, _: Button):
        player = self._get_player(interaction)
        await interaction.response.defer(ephemeral=True)
        
        if player:
            player.stop()
            await player.update_control_message()
    
    @discord.ui.button(label="🔉 Тише", style=ButtonStyle.secondary, custom_id="mp_quieter")
    async def volume_down(self, interaction: Interaction, _: Button):
        player = self._get_player(interaction)
        await interaction.response.defer(ephemeral=True)
        
        if player:
            player.volume = max(0.0, round(player.volume - 0.1, 2))
            if player.current_source:
                player.current_source.volume = player.volume
            await player.update_control_message()
    
    @discord.ui.button(label="🔊 Громче", style=ButtonStyle.secondary, custom_id="mp_louder")
    async def volume_up(self, interaction: Interaction, _: Button):
        player = self._get_player(interaction)
        await interaction.response.defer(ephemeral=True)
        
        if player:
            player.volume = min(2.0, round(player.volume + 0.1, 2))
            if player.current_source:
                player.current_source.volume = player.volume
            await player.update_control_message()
    
    @discord.ui.button(label="🛑 Стоп", style=ButtonStyle.danger, custom_id="mp_stop")
    async def hard_stop(self, interaction: Interaction, _: Button):
        player = self._get_player(interaction)
        await interaction.response.defer(ephemeral=True)
        
        if player:
            player.stop()
            await player.stop_and_cleanup()
            music_players.pop(interaction.guild.id, None)


# ============================================================================
# МОДАЛКИ ДЛЯ ЗАЯВОК
# ============================================================================

def build_recruitment_embed(guild: discord.Guild | None) -> discord.Embed:
    desc = (
        "Мы ищем новых ребят в нашу команду 🌟\n\n"
        "Хотите быть хелпером, ведущим или креативщиком ивентов?\n"
        "Заполните заявку — и у вас будет шанс присоединиться к нам!\n\n"
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
        
        embed = discord.Embed(
            title="📩 Новая заявка",
            description=desc,
            color=discord.Color.green()
        )
        embed.set_footer(text=f"ID: {interaction.user.id}")
        
        await admin_channel.send(embed=embed)
        await interaction.response.send_message("✅ Ваша заявка отправлена администрации!", ephemeral=True)


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


class ApplicationView(View):
    def __init__(self, roles_with_desc: List[Tuple[str, str]], target_channel_id: Optional[int]):
        super().__init__(timeout=None)
        self.add_item(RoleSelect(roles_with_desc, target_channel_id))


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


# ============================================================================
# МОДАЛКИ ДЛЯ ВРЕМЕННЫХ ВОЙСОВ
# ============================================================================

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
        
        for m in self.MENTION_RE.findall(text):
            try:
                ids.add(int(m))
            except ValueError:
                pass
        
        for token in re.split(r"[,\s]+", text.strip()):
            if token.isdigit():
                try:
                    ids.add(int(token))
                except ValueError:
                    pass
        
        members: list[discord.Member] = []
        for uid in ids:
            m = guild.get_member(uid)
            if m:
                members.append(m)
        return members
    
    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("❌ Это не ваш канал!", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True, thinking=False)
        
        guild = interaction.guild
        members = self._parse_members(guild, self.users_input.value)
        
        if not members:
            await interaction.followup.send("❌ Пользователи не найдены. Укажите @упоминания или ID.", ephemeral=True)
            return
        
        ok: list[str] = []
        failed_dm: list[str] = []
        failed_perm: list[str] = []
        
        for m in members:
            try:
                await self.voice_channel.set_permissions(
                    m,
                    view_channel=True,
                    connect=True,
                    speak=True
                )
            except (discord.Forbidden, discord.HTTPException):
                failed_perm.append(m.mention)
                continue
            
            try:
                jump_url = f"https://discord.com/channels/{guild.id}/{self.voice_channel.id}"
                dm_text = (
                    f"👋 Привет! {interaction.user.mention} приглашает тебя в голосовой канал "
                    f"**{self.voice_channel.name}** на сервере **{guild.name}**.\n"
                    f"Перейти: {jump_url}"
                )
                await m.send(dm_text)
                ok.append(m.mention)
            except (discord.Forbidden, discord.HTTPException):
                failed_dm.append(m.mention)
        
        parts = []
        if ok:
            parts.append(f"✅ Права выданы и приглашение отправлено: {', '.join(ok)}")
        if failed_perm:
            parts.append(f"⚠️ Не удалось выдать права: {', '.join(failed_perm)}")
        if failed_dm:
            parts.append(f"🔭 ЛС закрыты/не доставлено: {', '.join(failed_dm)}")
        
        if not parts:
            parts.append("❌ Никого пригласить не удалось.")
        
        await interaction.followup.send("\n".join(parts), ephemeral=True)


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
        await interaction.response.send_message(f"🎙 Меню управления каналом **{voice_channel.name}**", view=view, ephemeral=True)


# ============================================================================
# ТИКЕТЫ
# ============================================================================

class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="🎫 Создать тикет", style=discord.ButtonStyle.primary, custom_id="create_ticket")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        
        category = discord.utils.get(guild.categories, name="Tickets")
        if not category:
            try:
                category = await guild.create_category("Tickets", reason="Категория для тикетов")
            except discord.Forbidden:
                return await interaction.response.send_message("❌ У меня нет прав создать категорию.", ephemeral=True)
        
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        
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
        is_owner = interaction.user in interaction.channel.members
        is_server_owner = interaction.user.id == interaction.guild.owner_id
        
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


# ============================================================================
# КОМАНДЫ - МУЗЫКА
# ============================================================================

@bot.tree.command(name="play", description="Воспроизвести музыку")
@app_commands.describe(query="Название трека или ссылка")
async def play_cmd(interaction: Interaction, query: str):
    await interaction.response.defer()
    
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send("❌ Сначала зайдите в голосовой канал!", ephemeral=True)
        return
    
    voice_channel = interaction.user.voice.channel
    guild = interaction.guild
    guild_id = guild.id
    
    vc = discord.utils.get(bot.voice_clients, guild=guild)
    if not vc:
        vc = await voice_channel.connect()
    elif vc.channel != voice_channel:
        await vc.move_to(voice_channel)
    
    for _ in range(50):
        if vc.is_connected():
            break
        await asyncio.sleep(0.1)
    
    if not vc.is_connected():
        await interaction.followup.send("❌ Не удалось подключиться к голосовому каналу", ephemeral=True)
        return
    
    player = music_players.get(guild_id)
    if not player:
        player = MusicPlayer(guild, vc, interaction.channel)
        music_players[guild_id] = player
    else:
        player.vc = vc
        player.text_channel = interaction.channel
    
    success, message = await player.add_track(query)
    
    if not success:
        await interaction.followup.send(message, ephemeral=True)
        return
    
    if not vc.is_playing() and not vc.is_paused():
        await player.play_next()
    else:
        await player.update_control_message()
        await interaction.followup.send(message, ephemeral=True)


@bot.tree.command(name="queue", description="Показать очередь")
@app_commands.describe(page="Номер страницы")
async def queue_cmd(interaction: Interaction, page: Optional[int] = 1):
    await interaction.response.defer(ephemeral=True)
    
    player = music_players.get(interaction.guild.id)
    if not player or (not player.queue and not player.current_track):
        await interaction.followup.send("Очередь пуста", ephemeral=True)
        return
    
    page = max(1, page or 1)
    per_page = 20
    start = (page - 1) * per_page
    end = start + per_page
    
    lines = []
    if player.current_track:
        lines.append(f"**Сейчас:** {player.current_track[0]}")
    
    if player.queue:
        for i, track in enumerate(player.queue[start:end], start=start + 1):
            lines.append(f"{i}. {track[0]}")
    
    text = "\n".join(lines) or "Очередь пуста"
    total_pages = max(1, (len(player.queue) + per_page - 1) // per_page)
    
    await interaction.followup.send(f"{text}\n\nСтр. {page}/{total_pages}", ephemeral=True)


@bot.tree.command(name="skip", description="Пропустить текущий трек")
async def skip_cmd(interaction: Interaction):
    await interaction.response.defer(ephemeral=True)
    
    player = music_players.get(interaction.guild.id)
    if not player or not player.vc:
        await interaction.followup.send("❌ Ничего не играет", ephemeral=True)
        return
    
    player.stop()
    await player.update_control_message()
    await interaction.followup.send("⏭ Пропущено", ephemeral=True)


@bot.tree.command(name="pause", description="Пауза/Продолжить воспроизведение")
async def pause_cmd(interaction: Interaction):
    await interaction.response.defer(ephemeral=True)
    
    player = music_players.get(interaction.guild.id)
    if not player or not player.vc:
        await interaction.followup.send("❌ Ничего не играет", ephemeral=True)
        return
    
    if player.vc.is_paused():
        player.resume()
        msg = "▶️ Продолжаю"
    elif player.vc.is_playing():
        player.pause()
        msg = "⏸ Пауза"
    else:
        msg = "❌ Ничего не играет"
    
    await player.update_control_message()
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="remove", description="Удалить трек из очереди")
@app_commands.describe(index="Номер трека (см. /queue)")
async def remove_cmd(interaction: Interaction, index: int):
    await interaction.response.defer(ephemeral=True)
    
    player = music_players.get(interaction.guild.id)
    if not player or not player.queue:
        await interaction.followup.send("Очередь пуста", ephemeral=True)
        return
    
    if index < 1 or index > len(player.queue):
        await interaction.followup.send("Неверный номер", ephemeral=True)
        return
    
    title = player.queue.pop(index - 1)[0]
    await player.update_control_message()
    await interaction.followup.send(f"🗑 Удалён: **{title}**", ephemeral=True)


@bot.tree.command(name="stop", description="Остановить и очистить очередь")
async def stop_cmd(interaction: Interaction):
    await interaction.response.defer(ephemeral=True)
    
    player = music_players.get(interaction.guild.id)
    if not player:
        await interaction.followup.send("Уже остановлено", ephemeral=True)
        return
    
    try:
        player.stop()
        await player.stop_and_cleanup()
    finally:
        music_players.pop(interaction.guild.id, None)
    
    await interaction.followup.send("🛑 Остановлено и очищено", ephemeral=True)


# ============================================================================
# КОМАНДЫ - АДМИН
# ============================================================================

@bot.tree.command(name="set_admin_roles", description="Задать роли для админ-команд")
@app_commands.describe(role_names="Через запятую укажите роли")
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


@bot.tree.command(name="заявки", description="Создать панель заявок")
@app_commands.describe(channel="Канал, куда будут приходить заявки")
@is_admin()
async def заявки(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    target_channel_id = channel.id if channel else None
    await interaction.response.send_modal(ApplicationSetupModal(target_channel_id))


@bot.tree.command(name="тикеты", description="Создать сообщение с кнопкой тикета")
@is_admin()
async def тикеты(interaction: discord.Interaction):
    perms = interaction.channel.permissions_for(interaction.guild.me)
    if not (perms.send_messages and perms.embed_links):
        return await interaction.response.send_message("❌ У меня нет прав отправлять сообщения/вставлять embed здесь.", ephemeral=True)
    
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


@bot.tree.command(name="set_support_roles", description="Задать роли поддержки")
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


@bot.tree.command(name="setup_voice", description="Настроить канал для создания временных голосовых")
@app_commands.describe(
    trigger_channel="Канал, при входе в который создаётся временный голосовой",
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
    
    if category is None:
        category = discord.utils.get(guild.categories, name="Temporary Voice")
        if category is None:
            try:
                category = await guild.create_category("Temporary Voice", reason="Категория для временных войсов")
            except discord.Forbidden:
                return await interaction.response.send_message(
                    "❌ У меня нет прав создать категорию.", ephemeral=True
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


@bot.tree.command(name="панель_войса", description="Отправить меню управления временными войсами")
@app_commands.describe(channel="Канал, куда отправить меню")
@is_admin()
async def панель_войса(interaction: discord.Interaction, channel: discord.TextChannel):
    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.send_messages and perms.embed_links):
        return await interaction.response.send_message(
            f"❌ У меня нет прав отправлять сообщения/вставлять embed в {channel.mention}.",
            ephemeral=True
        )
    
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


@bot.tree.command(name="setup_welcome", description="Выбрать канал для приветствия")
@app_commands.describe(channel="Канал, куда будет отправляться приветствие")
@is_admin()
async def setup_welcome(interaction: discord.Interaction, channel: discord.TextChannel):
    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.send_messages and perms.embed_links):
        return await interaction.response.send_message(
            f"❌ У меня нет прав отправлять сообщения/вставлять embed в {channel.mention}.",
            ephemeral=True
        )
    
    st = welcome_settings.setdefault(interaction.guild.id, {})
    st["channel_id"] = channel.id
    st.setdefault("message", DEFAULT_WELCOME)
    st.setdefault("use_banner", True)
    st.setdefault("image_url", "")
    
    await interaction.response.send_message(
        f"✅ Канал для приветствий установлен: {channel.mention}",
        ephemeral=True
    )


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
    await interaction.followup.send(embed=_build_welcome_embed(interaction.guild, preview), ephemeral=True)


@bot.tree.command(name="clear", description="Удалить сообщения в канале")
@app_commands.describe(amount="Количество сообщений (1-100)")
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


@bot.tree.command(name="setlog", description="Установить канал для логов")
@app_commands.describe(channel="Канал для логов")
@is_admin()
async def setlog(interaction: discord.Interaction, channel: discord.TextChannel):
    log_channels[interaction.guild.id] = channel.id
    await interaction.response.send_message(
        f"✅ Канал для логов установлен на {channel.mention}", ephemeral=True
    )


# ============================================================================
# КОМАНДЫ - МОДЕРАЦИЯ С РАНГАМИ
# ============================================================================

@bot.tree.command(name="set_role_rank", description="(Владелец) Задать ранг роли для мод-команд (0-3)")
@app_commands.describe(
    role="Роль (упоминание/ID/имя)",
    rank="0 = снять; 1 = warn; 2 = mute/unmute; 3 = ban/unban"
)
async def set_role_rank(interaction: discord.Interaction, role: str, rank: int):
    if interaction.user.id != interaction.guild.owner_id:
        return await interaction.response.send_message("❌ Только владелец сервера.", ephemeral=True)
    if rank < 0 or rank > 3:
        return await interaction.response.send_message("❌ Ранг должен быть от 0 до 3.", ephemeral=True)
    
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


@bot.tree.command(name="warn", description="Выдать предупреждение пользователю")
@requires_rank(1)
@app_commands.describe(user="Кому выдать предупреждение", reason="Причина")
async def warn_cmd(interaction: discord.Interaction, user: discord.Member, reason: Optional[str] = None):
    await interaction.response.send_message(
        f"⚠️ {user.mention} получил предупреждение. Причина: {reason or 'не указана'}",
        ephemeral=True
    )


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
@app_commands.describe(member="Кого замьютить", minutes="На сколько минут (по умолчанию 10)", reason="Причина")
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
@app_commands.describe(member="С кого снять мут", reason="Причина")
async def unmute_cmd(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = None):
    role = discord.utils.get(interaction.guild.roles, name="Muted")
    if role is None or role not in member.roles:
        return await interaction.response.send_message("ℹ️ Этот участник не замьючен.", ephemeral=True)
    try:
        await member.remove_roles(role, reason=reason or f"Unmute by {interaction.user}")
        await interaction.response.send_message(f"📈 Мут снят с {member.mention}.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Нет прав снять мут.", ephemeral=True)


@bot.tree.command(name="ban", description="Забанить пользователя")
@requires_rank(3)
@app_commands.describe(
    user="Кого забанить",
    reason="Причина",
    delete_message_days="Удалить сообщения за N дней (0-7)"
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
    
    if query.isdigit():
        uid = int(query)
        for e in bans:
            if e.user.id == uid:
                target_entry = e
                break
    if not target_entry and "#" in query:
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


# ============================================================================
# LOCK/UNLOCK CHAT
# ============================================================================

def _ensure_snapshot(guild_id: int):
    return LOCK_SNAPSHOTS.setdefault(guild_id, {})


def _get_channel_snapshot(guild_id: int, channel_id: int):
    return LOCK_SNAPSHOTS.get(guild_id, {}).get(channel_id)


@bot.tree.command(name="lock_chat", description="(Владелец) Закрыть чат для всех")
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
    
    prev_every = snap.get("everyone", None)
    owe = ch.overwrites_for(interaction.guild.default_role)
    owe.send_messages = prev_every
    try:
        await ch.set_permissions(interaction.guild.default_role, overwrite=owe)
    except (discord.Forbidden, discord.HTTPException):
        pass
    
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
    
    try:
        del LOCK_SNAPSHOTS[interaction.guild.id][ch.id]
        if not LOCK_SNAPSHOTS[interaction.guild.id]:
            del LOCK_SNAPSHOTS[interaction.guild.id]
    except KeyError:
        pass
    
    await interaction.response.send_message(f"🔓 Канал {ch.mention} открыт, права восстановлены.", ephemeral=True)


# ============================================================================
# АНТИСПАМ
# ============================================================================

async def mute_user(member: discord.Member, guild: discord.Guild, context_channel: discord.TextChannel):
    """Мьютит пользователя"""
    role = await setup_muted_role(guild)
    
    if role in member.roles:
        return
    
    await member.add_roles(role, reason="Автоматический мут за спам")
    
    try:
        await context_channel.send(f"🔇 {member.mention} был автоматически замьючен на 2 часа за спам.")
    except Exception as e:
        logging.error(f"Ошибка при отправке сообщения в канал: {e}")
    
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
    
    history = user_message_history.get(user_id, [])
    history = [timestamp for timestamp in history if now - timestamp < SPAM_TIME_WINDOW]
    history.append(now)
    user_message_history[user_id] = history
    
    if len(history) >= SPAM_THRESHOLD:
        if not any(role.name == "Muted" for role in message.author.roles):
            await mute_user(message.author, message.guild, message.channel)
            user_message_history[user_id] = []
    
    await bot.process_commands(message)


# ============================================================================
# СОБЫТИЯ
# ============================================================================

@bot.event
async def on_ready():
    logging.info(f"✅ Бот {bot.user} запущен!")
    activity = discord.Game(name="/help ❤")
    await bot.change_presence(status=discord.Status.online, activity=activity)
    
    bot.add_view(MusicControlView())
    bot.add_view(TicketView())
    bot.add_view(ControlMenuView())
    
    try:
        synced = await tree.sync()
        logging.info(f"📄 Синхронизировано {len(synced)} команд")
    except Exception as e:
        logging.error(f"Ошибка sync: {e}")


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
    
    if after.channel and after.channel.id == trigger_id:
        category = guild.get_channel(category_id)
        if isinstance(category, discord.CategoryChannel):
            try:
                temp_vc = await category.create_voice_channel(f"{member.display_name}'s VC")
                user_temp_vcs[(guild_id, user_id)] = temp_vc.id
                await member.move_to(temp_vc)
            except discord.Forbidden:
                pass
            except discord.HTTPException:
                pass
    
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
        log_text += content[:500]
    if attachments:
        if log_text:
            log_text += "\n"
        log_text += f"🔎 Вложения: {attachments}"
    
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
        return
    await log(
        before.guild,
        f"✏️ Сообщение от {before.author.mention} отредактировано в {before.channel.mention}:\n"
        f"Было: > {before_content[:500]}\n"
        f"Стало: > {after_content[:500]}"
    )


@bot.event
async def on_member_update(before, after):
    if before.nick != after.nick:
        await log(before.guild, f"📝 У пользователя {before.mention} изменился ник с '{before.nick}' на '{after.nick}'")


@bot.event
async def on_member_ban(guild, user):
    await log(guild, f"⛔ Пользователь {user.name}#{user.discriminator} был забанен.")


@bot.event
async def on_member_unban(guild, user):
    await log(guild, f"✅ Пользователь {user.name}#{user.discriminator} был разбанен.")


# ============================================================================
# ОБРАБОТКА ОШИБОК
# ============================================================================

@warn_cmd.error
@mute_cmd.error
@unmute_cmd.error
@ban_cmd.error
@unban_cmd.error
async def _rank_check_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("❌ Недостаточный ранг для этой команды.", ephemeral=True)


@заявки.error
@тикеты.error
@панель_войса.error
@setup_welcome.error
@set_welcome_message.error
@slash_clear.error
@setlog.error
@set_support_roles.error
@setup_voice.error
async def _admin_check_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        if interaction.response.is_done():
            await interaction.followup.send("❌ У вас нет доступа к этой команде.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ У вас нет доступа к этой команде.", ephemeral=True)


# ============================================================================
# ЗАПУСК
# ============================================================================

if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    if TOKEN == "YOUR_TOKEN_HERE":
        print("⚠️ Установите переменную окружения DISCORD_TOKEN или вставьте токен в код!")
    else:
        bot.run(TOKEN)