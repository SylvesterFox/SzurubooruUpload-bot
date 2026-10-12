"""Telegram uploader for a local Szurubooru instance."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("szurubooru_bot")

ALLOWED_EXTENSIONS = {".jpg", ".png", ".webm", ".mp4", ".swf", ".avif", ".heif", ".heic"}
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
INSTANCE_URL = os.getenv("SZURU_URL", "http://127.0.0.1:8080").rstrip("/")
API_PREFIX = os.getenv("SZURU_API_PREFIX", "/api").strip("/")
SZURU_USERNAME = os.getenv("SZURU_USERNAME", "").strip()
SZURU_TOKEN = os.getenv("SZURU_TOKEN", "").strip()
SZURU_PASSWORD = os.getenv("SZURU_PASSWORD", "")
SAFETY = os.getenv("DEFAULT_SAFETY", "safe").strip().lower()
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "200"))
MAX_LINK_FILES = int(os.getenv("MAX_LINK_FILES", "10"))
FUZZYSEARCH_API_KEY = os.getenv("FUZZYSEARCH_API_KEY", "").strip()
FUZZYSEARCH_URL = os.getenv("FUZZYSEARCH_URL", "https://api.fuzzysearch.net/v1").rstrip("/")
E621_USER_AGENT = os.getenv(
    "E621_USER_AGENT",
    "szurubooru-telegram-uploader/1.0 (https://github.com/SylvesterFox/szurubooru_owoplus)",
).strip()
GALLERY_DL_COOKIES_FILE = os.path.expandvars(os.path.expanduser(os.getenv("GALLERY_DL_COOKIES_FILE", "").strip()))
if GALLERY_DL_COOKIES_FILE and not Path(GALLERY_DL_COOKIES_FILE).is_absolute():
    GALLERY_DL_COOKIES_FILE = str(Path(__file__).resolve().parent / GALLERY_DL_COOKIES_FILE)
GALLERY_DL_COOKIES_FROM_BROWSER = os.getenv("GALLERY_DL_COOKIES_FROM_BROWSER", "").strip()
ALLOWED_USERS = {int(value) for value in re.split(r"[,\s]+", os.getenv("TELEGRAM_ALLOWED_USERS", "").strip()) if value}
ALLOWED_LINK_HOSTS = {"bsky.app", "x.com", "www.x.com", "twitter.com", "www.twitter.com", "e621.net", "www.e621.net"}


def extract_link(text: str) -> str | None:
    match = re.search(r"https?://[^\s<>]+", text or "")
    if not match:
        return None
    url = match.group(0).rstrip(".,!?:;)]}")
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_LINK_HOSTS or parsed.username or parsed.password:
        raise ValueError("Принимаются ссылки только с bsky.app, x.com/twitter.com и e621.net (HTTPS).")
    return url


def caption_tags(text: str) -> list[str]:
    """Treat words and #hashtags as tags; omit URLs and remove the # prefix."""
    text = re.sub(r"https?://[^\s<>]+", " ", text or "")
    tags = []
    for word in re.split(r"[\s,]+", text):
        tag = word.lstrip("#").strip(".,!?:;()[]{}")
        if tag and not tag.startswith("#"):
            tags.append(tag)
    return list(dict.fromkeys(tags))


def is_allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USERS)


async def deny(update: Update) -> None:
    if update.effective_message:
        await update.effective_message.reply_text("Доступ запрещён. Добавьте свой Telegram ID в TELEGRAM_ALLOWED_USERS.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await deny(update)
        return
    await update.effective_message.reply_text(
        "Отправьте файл как документ или медиа. Поддерживаются JPG, PNG, WEBM, MP4, SWF, AVIF, HEIF и HEIC.\n"
        "Подпись используется как список тегов через пробел. Без тегов запись создаётся без тегов.\n"
        "Команды: /help"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


def api_auth():
    if SZURU_USERNAME and SZURU_TOKEN:
        credential = base64.b64encode(f"{SZURU_USERNAME}:{SZURU_TOKEN}".encode()).decode()
        return {"Authorization": f"Token {credential}"}, None
    if SZURU_USERNAME and SZURU_PASSWORD:
        return {}, (SZURU_USERNAME, SZURU_PASSWORD)
    raise RuntimeError("Укажите SZURU_USERNAME и SZURU_TOKEN либо SZURU_PASSWORD в .env")


def find_e621_tags(name: str, data: bytes) -> tuple[list[str], str | None]:
    """Reverse-search supported images with FuzzySearch and fetch e621 tags."""
    image_extensions = {".jpg", ".jpeg", ".png", ".avif", ".heif", ".heic"}
    if not FUZZYSEARCH_API_KEY or Path(name).suffix.lower() not in image_extensions:
        return [], None
    if len(data) > 10 * 1024 * 1024:
        log.info("Skipping FuzzySearch for %s: image exceeds 10 MB", name)
        return [], None

    response = httpx.post(
        f"{FUZZYSEARCH_URL}/image",
        params={"type": "close"},
        headers={"x-api-key": FUZZYSEARCH_API_KEY},
        files={"image": (Path(name).name, data, "application/octet-stream")},
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, list):
        matches = payload
    elif isinstance(payload, dict):
        matches = payload.get("matches", payload.get("results", []))
    else:
        raise RuntimeError("FuzzySearch вернул ответ неожиданного формата.")
    if not isinstance(matches, list):
        raise RuntimeError("В ответе FuzzySearch не найден список совпадений.")

    for match in matches:
        if not isinstance(match, dict):
            continue
        match_url = str(match.get("url") or "")
        hostname = (urlparse(match_url).hostname or "").lower()
        site = str(match.get("site", "")).lower()
        site_info = match.get("site_info")
        if isinstance(site_info, str):
            site = site or site_info.lower()
        elif isinstance(site_info, dict) and site_info:
            site = site or str(next(iter(site_info))).lower()
        if site != "e621" and hostname not in {"e621.net", "www.e621.net"}:
            continue
        distance = match.get("distance")
        if distance is not None and float(distance) > 10:
            continue

        post_id = str(match.get("site_id_str") or match.get("site_id") or "")
        if not post_id.isdigit():
            post_match = re.search(r"/(?:posts|post/show)/(\d+)", urlparse(match_url).path)
            post_id = post_match.group(1) if post_match else ""
        if not post_id:
            continue

        result = httpx.get(
            f"https://e621.net/posts/{post_id}.json",
            headers={"User-Agent": E621_USER_AGENT},
            timeout=20,
        )
        result.raise_for_status()
        post = result.json().get("post", {})
        tag_groups = post.get("tags", {})
        tags = [tag for group in tag_groups.values() if isinstance(group, list) for tag in group]
        if tags:
            return list(dict.fromkeys(tags)), f"https://e621.net/posts/{post_id}"
    return [], None


def enrich_tags(name: str, data: bytes, tags: list[str], source: str | None) -> tuple[list[str], str | None]:
    try:
        found_tags, found_source = find_e621_tags(name, data)
    except Exception as exc:
        # Image lookup is best-effort; it must not block the user's upload.
        log.warning("FuzzySearch/e621 lookup failed for %s: %s", name, exc)
        return tags, source
    return list(dict.fromkeys(tags + found_tags)), source or found_source


def post_file(name: str, data: bytes, tags: list[str], source: str | None = None) -> dict:
    headers, auth = api_auth()
    api = f"{INSTANCE_URL}/{API_PREFIX}" if API_PREFIX else INSTANCE_URL
    source_host = (urlparse(source).hostname or "").lower() if source else ""
    if source_host in {"e621.net", "www.e621.net"}:
        tags = list(dict.fromkeys(tags))
    else:
        tags, source = enrich_tags(name, data, tags, source)
    metadata = {"tags": tags, "safety": SAFETY}
    if source:
        metadata["source"] = source
    response = httpx.post(
        f"{api}/posts/",
        headers={**headers, "Accept": "application/json"},
        auth=auth,
        data={},
        files={
            "metadata": ("metadata.json", json.dumps(metadata).encode(), "application/json"),
            "content": (Path(name).name, data, "application/octet-stream"),
        },
        timeout=180,
    )
    try:
        body = response.json()
    except ValueError:
        body = {"description": response.text[:500]}
    if not response.is_success:
        detail = body.get("description") or body.get("title") or response.reason_phrase
        raise RuntimeError(f"Szurubooru HTTP {response.status_code}: {detail}")
    return body


def download_e621_post(url: str, tags: list[str]) -> list[dict]:
    """Fetch an e621 post, its source file, and tags through the e621 API."""
    parsed = urlparse(url)
    match = re.fullmatch(r"/posts/(\d+)/?", parsed.path)
    if not match:
        raise RuntimeError("Ожидается ссылка вида https://e621.net/posts/123456.")
    post_id = match.group(1)
    headers = {"User-Agent": E621_USER_AGENT}

    response = httpx.get(f"https://e621.net/posts/{post_id}.json", headers=headers, timeout=30)
    response.raise_for_status()
    payload = response.json()
    post = payload.get("post", payload) if isinstance(payload, dict) else {}
    file_info = post.get("file") or {}
    file_url = file_info.get("url")
    if not file_url:
        raise RuntimeError("e621 не вернул файл поста: возможно, пост удалён или недоступен.")

    file_parsed = urlparse(file_url)
    if file_parsed.scheme != "https" or not (
        file_parsed.hostname == "e621.net" or (file_parsed.hostname or "").endswith(".e621.net")
    ):
        raise RuntimeError("e621 вернул неподдерживаемый адрес файла.")
    filename = Path(file_parsed.path).name or f"{post_id}.{file_info.get('ext', 'jpg')}"
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise RuntimeError(f"Формат e621 {extension or 'без расширения'} не поддерживается ботом.")

    media_response = httpx.get(file_url, headers=headers, timeout=120)
    media_response.raise_for_status()
    data = media_response.content
    if len(data) > MAX_FILE_MB * 1024 * 1024:
        raise RuntimeError(f"Файл e621 больше ограничения {MAX_FILE_MB} МБ.")

    tag_groups = post.get("tags") or {}
    e621_tags = [tag for group in tag_groups.values() if isinstance(group, list) for tag in group]
    merged_tags = list(dict.fromkeys(tags + e621_tags))
    canonical_source = f"https://e621.net/posts/{post_id}"
    return [post_file(filename, data, merged_tags, source=canonical_source)]


def download_link_posts(url: str, tags: list[str]) -> list[dict]:
    """Download media from one supported post and upload each file with its source."""
    if (urlparse(url).hostname or "").lower() in {"e621.net", "www.e621.net"}:
        return download_e621_post(url, tags)
    if not shutil.which("gallery-dl"):
        raise RuntimeError("Не найден gallery-dl. Установите зависимости из requirements.txt.")
    if GALLERY_DL_COOKIES_FILE and GALLERY_DL_COOKIES_FROM_BROWSER:
        raise RuntimeError("Задайте только один параметр авторизации gallery-dl: файл cookies или профиль браузера.")
    if GALLERY_DL_COOKIES_FILE and not Path(GALLERY_DL_COOKIES_FILE).is_file():
        raise RuntimeError(f"Файл cookies не найден: {GALLERY_DL_COOKIES_FILE}")
    with tempfile.TemporaryDirectory(prefix="szuru-link-") as temporary_directory:
        command = ["gallery-dl", "--no-mtime", "--range", f"1-{MAX_LINK_FILES}", "--directory", temporary_directory]
        if GALLERY_DL_COOKIES_FILE:
            command.extend(["--cookies", GALLERY_DL_COOKIES_FILE])
        if GALLERY_DL_COOKIES_FROM_BROWSER:
            command.extend(["--cookies-from-browser", GALLERY_DL_COOKIES_FROM_BROWSER])
        command.append(url)
        result = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
        files = sorted(
            path for path in Path(temporary_directory).rglob("*")
            if path.is_file() and path.suffix.lower() in ALLOWED_EXTENSIONS
        )
        if result.returncode != 0 and not files:
            detail = (result.stderr or result.stdout or "Не удалось разобрать страницу.").strip()
            raise RuntimeError(detail[-700:])
        if not files:
            raise RuntimeError("На странице не найдено файлов поддерживаемых форматов.")
        if len(files) > MAX_LINK_FILES:
            raise RuntimeError(f"На странице найдено {len(files)} файлов; лимит одной ссылки — {MAX_LINK_FILES}.")
        posts = []
        for path in files:
            size = path.stat().st_size
            if size > MAX_FILE_MB * 1024 * 1024:
                raise RuntimeError(f"Файл {path.name} больше ограничения {MAX_FILE_MB} МБ.")
            posts.append(post_file(path.name, path.read_bytes(), tags, source=url))
        return posts


async def handle_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await deny(update)
        return
    message = update.effective_message
    if not message:
        return

    link_text = message.caption or message.text or ""
    try:
        source_url = extract_link(link_text)
    except ValueError as exc:
        await message.reply_text(str(exc))
        return

    telegram_file = None
    filename = ""
    if message.document:
        telegram_file = await message.document.get_file()
        filename = message.document.file_name or "upload"
    elif message.video:
        telegram_file = await message.video.get_file()
        filename = message.video.file_name or "video.mp4"
    elif message.animation:
        telegram_file = await message.animation.get_file()
        filename = message.animation.file_name or "animation.mp4"
    elif message.photo:
        telegram_file = await message.photo[-1].get_file()
        filename = "image.jpg"
    elif not source_url:
        return

    tags = caption_tags(link_text)
    status = await message.reply_text("Скачиваю и загружаю публикацию…" if source_url and telegram_file is None else "Загружаю в Szurubooru…")
    if telegram_file is None:
        try:
            posts = await context.application.bot_data["upload_semaphore"].run(
                lambda: download_link_posts(source_url, tags)
            )
            ids = ", ".join(str(post.get("id", "?")) for post in posts)
            await status.edit_text(f"Загружено постов: {len(posts)} (#{ids}). Источник сохранён.")
        except Exception as exc:
            log.exception("Link import failed for %s", source_url)
            await status.edit_text(f"Не удалось загрузить ссылку: {str(exc)[:800]}")
        return

    if Path(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
        await message.reply_text("Формат не поддерживается. Отправьте JPG, PNG, WEBM, MP4, SWF, AVIF, HEIF или HEIC.")
        return
    if telegram_file.file_size and telegram_file.file_size > MAX_FILE_MB * 1024 * 1024:
        await message.reply_text(f"Файл больше ограничения {MAX_FILE_MB} МБ.")
        return

    try:
        payload = await telegram_file.download_as_bytearray()
        result = await context.application.bot_data["upload_semaphore"].run(
            lambda: post_file(filename, bytes(payload), tags, source=source_url)
        )
        post_id = result.get("id", "?")
        await status.edit_text(f"Опубликовано: пост #{post_id}")
    except Exception as exc:  # Give the sender the API's useful error text.
        log.exception("Upload failed for %s", filename)
        await status.edit_text(f"Не удалось загрузить: {str(exc)[:800]}")


class SemaphoreRunner:
    """Run blocking API calls off the event loop, one at a time."""

    def __init__(self):
        import asyncio
        self._semaphore = asyncio.Semaphore(2)

    async def run(self, operation):
        import asyncio
        async with self._semaphore:
            return await asyncio.to_thread(operation)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Укажите TELEGRAM_BOT_TOKEN в .env")
    if not ALLOWED_USERS:
        raise SystemExit("Укажите свой Telegram ID в TELEGRAM_ALLOWED_USERS в .env")
    if SAFETY not in {"safe", "sketchy", "unsafe"}:
        raise SystemExit("DEFAULT_SAFETY должен быть safe, sketchy или unsafe")
    api_auth()
    app = Application.builder().token(BOT_TOKEN).build()
    app.bot_data["upload_semaphore"] = SemaphoreRunner()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.ANIMATION | (filters.TEXT & ~filters.COMMAND), handle_upload))
    log.info("Starting uploader for %s", INSTANCE_URL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
