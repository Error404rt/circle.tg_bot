import asyncio
import os
import subprocess
import tempfile
import logging
from datetime import datetime
from collections import deque
from typing import Dict
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message, FSInputFile
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
import yt_dlp

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = "8284574123:AAHLqnW_v6a6xix4DQ1Czu3YyijWptvB4pw"

bot = Bot(token=TOKEN)
dp = Dispatcher()

MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_NOTE_DURATION = 60

# Очередь для обработки (для многих пользователей)
task_queue = deque()
AVERAGE_PROCESSING_TIME = 30  # Секунд на задачу
MAX_CONCURRENT_TASKS = 1  # Только 1 задача одновременно, остальные в очередь

class DownloadStates(StatesGroup):
    waiting_for_tt_url = State()
    waiting_for_yt_url = State()

async def process_queue(user_id: int, task: callable, message: Message):
    """Обработка очереди"""
    task_queue.append((user_id, task, message))
    queue_position = len([t for t in task_queue if t[0] == user_id])
    # Очередь начинается с 2 пользователей (1 работает, 1 ждёт)
    if len(task_queue) > 1:
        wait_time = (queue_position - 1) * AVERAGE_PROCESSING_TIME
        minutes = int(wait_time // 60)
        seconds = int(wait_time % 60)
        await message.answer(f"🔍 Очередь (позиция {queue_position}). Ожидание: {minutes} мин {seconds} сек 🔍")
    while task_queue and task_queue[0][0] == user_id:
        current_task, current_message = task_queue.popleft()[1:]
        try:
            await current_task(current_message)
        except Exception as e:
            logger.error(f"Error in task: {e}")
            await current_message.answer("❌ Ошибка обработки.")
        await asyncio.sleep(1)  # Пауза для rate limiting

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "🤖 <b>Привет! Я твой бот!</b>\n\n"
        "🎥 <b>Могу:</b>\n"
        "• Делать кружки из видео\n"
        "• Скачивать с TikTok (/tt_v_d)\n"
        "• Скачивать с YouTube (/yt_v_d или /yt_v_d{качество}, включая Shorts)\n\n"
        "📝 Отправь видео или ссылку!",
        parse_mode="HTML"
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📋 <b>Команды:</b>\n\n"
        "🎥 <b>Видео:</b>\n"
        "• Отправь видео — получите кружок(и)\n\n"
        "📥 <b>Скачивание:</b>\n"
        "• /tt_v_d - TikTok без водяных знаков\n"
        "• /yt_v_d - YouTube (по умолчанию 720p, включая Shorts)\n"
        "• /yt_v_d{качество} (например, /yt_v_d720p)\n\n"
        "⚙️ <b>Другое:</b>\n"
        "• /start - начать\n"
        "• /help - помощь",
        parse_mode="HTML"
    )

@dp.message(Command("tt_v_d"))
async def cmd_tt_download(message: Message, state: FSMContext):
    await message.answer("📥 Отправь ссылку на TikTok")
    await state.set_state(DownloadStates.waiting_for_tt_url)

@dp.message(lambda message: message.text and message.text.startswith('/yt_v_d'))
async def cmd_yt_download(message: Message, state: FSMContext):
    cmd = message.text.strip()
    quality = None
    if len(cmd) > 7:
        quality_str = cmd[7:].lower().replace('p', '')
        try:
            quality = int(quality_str)
        except ValueError:
            await message.answer("❌ Используй /yt_v_d720p")
            return
    await message.answer(f"📥 Отправь YouTube-ссылку{' (качество: ' + str(quality) + 'p)' if quality else ''}")
    await state.set_data({'yt_quality': quality})
    await state.set_state(DownloadStates.waiting_for_yt_url)

@dp.message(DownloadStates.waiting_for_yt_url)
async def handle_yt_url(message: Message, state: FSMContext):
    async def process_yt(message: Message):
        url = message.text.strip()
        if not url.startswith(('http://', 'https://')):
            await message.answer("❌ Неверная ссылка.")
            return
        data = await state.get_data()
        quality = data.get('yt_quality', 720)
        processing_msg = await message.answer(f"📥 Скачиваю YouTube в {quality}p...")
        temp_file = None
        max_retries = 3
        try:
            for attempt in range(max_retries):
                try:
                    temp_file = tempfile.mktemp(suffix='.mp4')
                    ydl_opts = {
                        'format': f'bestvideo[height<={quality}][vcodec!*=vp9]+bestaudio/best',  # Поддержка Shorts
                        'outtmpl': temp_file,
                        'quiet': True,
                        'no_warnings': True,
                        'noplaylist': True,
                        'extractor_args': {
                            'youtube': {
                                'player_skip': 'js',
                                'skip': ['dash', 'hls'],  # Обход ограничений Shorts
                            }
                        },
                        'retry_max': 3,  # Retry для YouTube rate limiting
                        'force_keyframes_at_cuts': True  # Улучшение обработки коротких видео
                    }
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([url])
                    if os.path.getsize(temp_file) > MAX_FILE_SIZE:
                        await message.answer("❌ Видео >50MB")
                        os.remove(temp_file)
                        return
                    await message.answer_video(FSInputFile(temp_file), caption=f"🎥 YouTube в {quality}p")
                    await processing_msg.edit_text("✅ Готово!")
                    break
                except yt_dlp.utils.DownloadError as e:
                    if "sign in required" in str(e).lower() or "429" in str(e):
                        await message.answer("❌ YouTube требует входа или rate limit. Попробуй позже или обнови yt-dlp.")
                    elif attempt < max_retries - 1:
                        await processing_msg.edit_text(f"🔍 Retry {attempt + 1}/{max_retries}...")
                        await asyncio.sleep(5)
                    else:
                        await message.answer("❌ Ошибка скачивания. Проверь ссылку или обнови yt-dlp.")
                    if temp_file and os.path.exists(temp_file):
                        os.remove(temp_file)
        finally:
            await state.clear()
            if temp_file and os.path.exists(temp_file):
                os.remove(temp_file)

    await process_queue(message.from_user.id, process_yt, message)

@dp.message(DownloadStates.waiting_for_tt_url)
async def handle_tt_url(message: Message, state: FSMContext):
    async def process_tt(message: Message):
        url = message.text.strip()
        if not url.startswith(('http://', 'https://')):
            await message.answer("❌ Неверная ссылка.")
            return
        processing_msg = await message.answer("📥 Скачиваю TikTok...")
        temp_file = None
        try:
            temp_file = tempfile.mktemp(suffix='.mp4')
            ydl_opts = {'format': 'best', 'outtmpl': temp_file, 'quiet': True, 'no_warnings': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                music_title = info.get('track', info.get('music', 'Неизвестно'))
                if isinstance(music_title, dict):
                    music_title = music_title.get('title', 'Неизвестно')
                ydl.download([url])
            if os.path.getsize(temp_file) > MAX_FILE_SIZE:
                await message.answer("❌ Видео >50MB")
                os.remove(temp_file)
                return
            await message.answer_video(FSInputFile(temp_file), caption=f"🎥 TikTok без водяных знаков\n🎵 Музыка: {music_title}")
            await processing_msg.edit_text("✅ Готово!")
        except Exception as e:
            logger.error(f"Error: {e}")
            await message.answer("❌ Ошибка скачивания.")
        finally:
            await state.clear()
            if temp_file and os.path.exists(temp_file):
                os.remove(temp_file)

    await process_queue(message.from_user.id, process_tt, message)

@dp.message(lambda message: message.video is not None)
async def handle_video(message: Message):
    async def process_video(message: Message):
        input_path = tempfile.mktemp(suffix='.mp4')
        try:
            file_id = message.video.file_id
            file = await bot.get_file(file_id)
            processing_msg = await message.answer("🔄 Обрабатываю...")
            await bot.download_file(file.file_path, destination=input_path)
            duration_str = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", input_path]).decode().strip()
            duration = float(duration_str)
            if duration <= MAX_NOTE_DURATION:
                output_path = tempfile.mktemp(suffix='.mp4')
                subprocess.run(["ffmpeg", "-y", "-i", input_path, "-vf", "crop=min(iw\\,ih):min(iw\\,ih),scale=240:240", "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-c:a", "aac", "-b:a", "64k", output_path], check=True)
                if os.path.getsize(output_path) > MAX_FILE_SIZE:
                    await message.answer("❌ Кружок >50MB")
                else:
                    await message.answer_video_note(FSInputFile(output_path))
                os.remove(output_path)
            else:
                num_parts = int(duration // MAX_NOTE_DURATION) + (1 if duration % MAX_NOTE_DURATION > 0 else 0)
                await processing_msg.edit_text(f"🔄 Делю на {num_parts} кружков...")
                for part in range(num_parts):
                    output_path = tempfile.mktemp(suffix='.mp4')
                    start_time = part * MAX_NOTE_DURATION
                    part_duration = min(MAX_NOTE_DURATION, duration - start_time)
                    subprocess.run(["ffmpeg", "-y", "-i", input_path, "-ss", str(start_time), "-t", str(part_duration), "-vf", "crop=min(iw\\,ih):min(iw\\,ih),scale=240:240", "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-c:a", "aac", "-b:a", "64k", output_path], check=True)
                    if os.path.getsize(output_path) > MAX_FILE_SIZE:
                        await message.answer(f"❌ Часть {part+1} >50MB")
                    else:
                        await message.answer_video_note(FSInputFile(output_path), caption=f"Часть {part+1}/{num_parts}")
                    os.remove(output_path)
            await processing_msg.edit_text("✅ Готово!")
        except Exception as e:
            logger.error(f"Error: {e}")
            await message.answer("❌ Ошибка обработки.")
        finally:
            if os.path.exists(input_path):
                os.remove(input_path)
            try:
                await processing_msg.delete()
            except:
                pass

    await process_queue(message.from_user.id, process_video, message)

@dp.message()
async def handle_other_messages(message: Message):
    text = message.text or ""
    if any(domain in text for domain in ["tiktok.com", "youtube.com", "youtu.be", "http://", "https://"]):
        await message.answer("🔗 <b>Ссылка!</b>\nИспользуй /tt_v_d или /yt_v_d.", parse_mode="HTML")
    else:
        await message.answer("🤖 <b>Не понимаю.</b>\nИспользуй /help или отправь видео!", parse_mode="HTML")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    print("🤖 Бот запускается...")
    try:
        subprocess.run(["ffmpeg", "-version"], check=True, capture_output=True)
        print("✅ FFmpeg найден")
    except:
        print("❌ FFmpeg не найден! Установи: pkg install ffmpeg")
        exit(1)
    try:
        subprocess.run(["yt-dlp", "--version"], check=True, capture_output=True)
        print("✅ yt-dlp найден")
    except:
        print("❌ yt-dlp не найден! Установи: pip install yt-dlp")
        exit(1)
    print("🚀 Бот запущен!")
    asyncio.run(main())
