"""Telegram bot (long polling). Only allowlisted users may use it."""

import asyncio
import logging
from pathlib import Path

from sqlalchemy import select
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from app.config import get_settings
from app.db.models import AuditEvent, User, VoiceMessage
from app.db.session import session_scope

logger = logging.getLogger(__name__)

REFUSAL = "متأسفم، این ربات خصوصی است و شما اجازهٔ استفاده از آن را ندارید."

START_TEXT = (
    "سلام! من دستیار صوتی شما هستم.\n"
    "یک پیام صوتی فارسی بفرستید تا جلسه‌ای در تقویم ثبت کنم یا ایمیلی از طرف شما بفرستم. "
    "هیچ کاری بدون تأیید شما انجام نمی‌شود.\n"
    "برای راهنما /help را بزنید."
)

HELP_TEXT = (
    "راهنما:\n"
    "• پیام صوتی: درخواستتان را به فارسی بگویید، مثلاً «فردا ساعت ده با علی جلسه بگذار».\n"
    "• پیام متنی: فعلاً برای آزمایش، همان متن را برمی‌گردانم.\n"
    "• /start شروع\n"
    "• /help همین راهنما"
)

ERROR_TEXT = "متأسفم، خطایی رخ داد. لطفاً دوباره تلاش کنید."


def _actor(update: Update) -> str:
    user = update.effective_user
    return f"telegram:{user.id}" if user else "telegram:unknown"


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every handler; stops updates from users not on the allowlist."""
    user = update.effective_user
    if user is not None and user.id in get_settings().allowed_ids:
        return
    logger.warning("Refused update from user %s", user.id if user else None)
    if update.effective_message is not None:
        await update.effective_message.reply_text(REFUSAL)
    raise ApplicationHandlerStop


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(START_TEXT)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT)


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(update.effective_message.text)


def _store_voice(
    telegram_user_id: int, name: str, file_id: str, file_path: str, duration_s: int
) -> int:
    with session_scope() as session:
        user = session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
        if user is None:
            user = User(telegram_user_id=telegram_user_id, name=name)
            session.add(user)
            session.flush()
        voice = VoiceMessage(
            user_id=user.id,
            telegram_file_id=file_id,
            file_path=file_path,
            duration_s=duration_s,
        )
        session.add(voice)
        session.flush()
        session.add(
            AuditEvent(
                event_type="voice_received",
                actor=f"telegram:{telegram_user_id}",
                payload_json={"voice_message_id": voice.id, "duration_s": duration_s},
            )
        )
        return voice.id


async def voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    tg_user = update.effective_user
    tg_voice = message.voice

    target_dir = Path(get_settings().voice_dir) / str(tg_user.id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{message.message_id}_{tg_voice.file_unique_id}.ogg"

    tg_file = await tg_voice.get_file()
    await tg_file.download_to_drive(custom_path=target)

    voice_id = await asyncio.to_thread(
        _store_voice,
        tg_user.id,
        tg_user.full_name,
        tg_voice.file_id,
        str(target),
        tg_voice.duration,
    )
    logger.info("Stored voice message %s (%ss)", voice_id, tg_voice.duration)
    await message.reply_text(
        f"پیام صوتی شما ({tg_voice.duration} ثانیه) دریافت و ذخیره شد. "
        "پردازش آن به‌زودی اضافه می‌شود."
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Error while handling an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        await update.effective_message.reply_text(ERROR_TEXT)


def build_application() -> Application:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    if not settings.allowed_ids:
        logger.warning("ALLOWED_USER_IDS is empty; every user will be refused")

    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(TypeHandler(Update, guard), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.VOICE, voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG if get_settings().debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every request URL, which contains the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)
