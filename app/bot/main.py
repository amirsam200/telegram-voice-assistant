"""Telegram bot (long polling). Only allowlisted users may use it.

Voice (or text) -> transcript -> agent -> Persian preview with Confirm / Edit / Cancel.
Only Confirm runs the action, and a run executes at most once.
"""

import asyncio
import logging
import re
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from app.agent.agent import run_agent
from app.agent.executor import cancel, confirm_and_execute, preview
from app.agent.provider import DeepSeekProvider, LLMError
from app.config import get_settings
from app.db.models import AgentRun, AuditEvent, RunStatus, User, VoiceMessage
from app.db.session import SessionLocal, session_scope
from app.stt.vosk_stt import SttError, transcribe_file
from app.tools.registry import ToolContext

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
    "• جلسه: «فردا ساعت ده صبح با علی جلسه بودجه بذار».\n"
    "• ایمیل: «به سارا ایمیل بزن و بگو گزارش آماده است».\n"
    "• پیام متنی هم مثل پیام صوتی پردازش می‌شود.\n"
    "پیش از انجام هر کاری پیش‌نمایش آن را می‌بینید و با «تأیید» اجرا می‌شود.\n"
    "• /start شروع\n"
    "• /help همین راهنما"
)

ERROR_TEXT = "متأسفم، خطایی رخ داد. لطفاً دوباره تلاش کنید."
REPEAT_TEXT = "متأسفم، صدایتان را واضح متوجه نشدم. لطفاً آرام‌تر و شمرده‌تر دوباره بگویید."
LLM_DOWN_TEXT = "سرویس هوش مصنوعی فعلاً در دسترس نیست. لطفاً چند دقیقه بعد دوباره تلاش کنید."
STT_FAILED_TEXT = "متأسفم، تبدیل صدا به متن ناموفق بود. لطفاً دوباره بفرستید یا بنویسید."
EDIT_PROMPT = "✏️ چه تغییری بدهم؟ با پیام صوتی یا متنی بگویید."
CANCELLED_TEXT = "❌ لغو شد. کاری انجام نشد."

CALLBACK = re.compile(r"^run:(\d+):(confirm|edit|cancel)$")


@lru_cache
def _provider() -> DeepSeekProvider:
    return DeepSeekProvider()


def _keyboard(run_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تأیید", callback_data=f"run:{run_id}:confirm"),
        InlineKeyboardButton("✏️ ویرایش", callback_data=f"run:{run_id}:edit"),
        InlineKeyboardButton("❌ لغو", callback_data=f"run:{run_id}:cancel"),
    ]])


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every handler; stops updates from users not on the allowlist."""
    user = update.effective_user
    if user is not None and user.id in get_settings().allowed_ids:
        return
    logger.warning("Refused update from user %s", user.id if user else None)
    if update.callback_query is not None:
        await update.callback_query.answer(REFUSAL, show_alert=True)
    elif update.effective_message is not None:
        await update.effective_message.reply_text(REFUSAL)
    raise ApplicationHandlerStop


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(START_TEXT)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT)


# --- database helpers (run in a worker thread) ----------------------------------------------


def _ensure_user(telegram_user_id: int, name: str) -> int:
    with session_scope() as session:
        user = session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
        if user is None:
            user = User(telegram_user_id=telegram_user_id, name=name)
            session.add(user)
            session.flush()
        return user.id


def _store_voice(user_id: int, telegram_user_id: int, file_id: str, file_path: str, duration_s: int) -> int:
    with session_scope() as session:
        voice = VoiceMessage(user_id=user_id, telegram_file_id=file_id, file_path=file_path, duration_s=duration_s)
        session.add(voice)
        session.flush()
        session.add(AuditEvent(
            event_type="voice_received",
            actor=f"telegram:{telegram_user_id}",
            payload_json={"voice_message_id": voice.id, "duration_s": duration_s},
        ))
        return voice.id


def _save_transcript(voice_id: int, text: str, confidence: float) -> None:
    with session_scope() as session:
        voice = session.get(VoiceMessage, voice_id)
        voice.transcript = text
        voice.stt_confidence = confidence
        voice.file_path = None


def _run_agent(user_id: int, text: str, voice_id: int | None, edit_run_id: int | None):
    session = SessionLocal()
    try:
        user = session.get(User, user_id)
        edit_of = session.get(AgentRun, edit_run_id) if edit_run_id else None
        if edit_of is not None and edit_of.user_id != user_id:
            edit_of = None
        ctx = ToolContext(session=session, user_id=user_id)
        result = run_agent(session, user, text, _provider(), ctx, voice_message_id=voice_id, edit_of=edit_of)
        if result.action is not None:
            reply = preview(result.action)
            session.get(AgentRun, result.run_id).reply_text = reply
            session.commit()
            return result.run_id, reply, True
        return result.run_id, result.question, False
    finally:
        session.close()


def _confirm(run_id: int, user_id: int):
    session = SessionLocal()
    try:
        return confirm_and_execute(session, run_id, user_id, ToolContext(session=session, user_id=user_id))
    finally:
        session.close()


def _cancel(run_id: int, user_id: int) -> bool:
    session = SessionLocal()
    try:
        return cancel(session, run_id, user_id)
    finally:
        session.close()


def _run_status(run_id: int, user_id: int):
    with session_scope() as session:
        run = session.get(AgentRun, run_id)
        return run.status if run is not None and run.user_id == user_id else None


# --- handlers --------------------------------------------------------------------------------


async def _handle_request(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str,
                          voice_id: int | None) -> None:
    message = update.effective_message
    await context.bot.send_chat_action(message.chat_id, ChatAction.TYPING)
    edit_run_id = context.user_data.pop("edit_run_id", None)
    try:
        run_id, reply, is_action = await asyncio.to_thread(_run_agent, user_id, text, voice_id, edit_run_id)
    except LLMError:
        await message.reply_text(LLM_DOWN_TEXT)
        return
    if is_action:
        await message.reply_text(reply, reply_markup=_keyboard(run_id))
    else:
        await message.reply_text(reply)


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user = update.effective_user
    user_id = await asyncio.to_thread(_ensure_user, tg_user.id, tg_user.full_name)
    await _handle_request(update, context, user_id, update.effective_message.text, None)


async def voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    tg_user = update.effective_user
    tg_voice = message.voice
    user_id = await asyncio.to_thread(_ensure_user, tg_user.id, tg_user.full_name)

    target_dir = Path(get_settings().voice_dir) / str(tg_user.id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{message.message_id}_{tg_voice.file_unique_id}.ogg"
    tg_file = await tg_voice.get_file()
    await tg_file.download_to_drive(custom_path=target)

    voice_id = await asyncio.to_thread(
        _store_voice, user_id, tg_user.id, tg_voice.file_id, str(target), tg_voice.duration
    )
    logger.info("Stored voice message %s (%ss)", voice_id, tg_voice.duration)
    await context.bot.send_chat_action(message.chat_id, ChatAction.TYPING)

    try:
        result = await asyncio.to_thread(transcribe_file, target)
    except SttError:
        logger.exception("Transcription failed for voice message %s", voice_id)
        await message.reply_text(STT_FAILED_TEXT)
        return
    finally:
        # Audio is deleted after transcription, whether or not it succeeded.
        target.unlink(missing_ok=True)
    await asyncio.to_thread(_save_transcript, voice_id, result.text, result.confidence)

    if not result.is_usable(get_settings().stt_min_confidence):
        await message.reply_text(REPEAT_TEXT)
        return
    await message.reply_text(f"🎙️ «{result.text}»")
    await _handle_request(update, context, user_id, result.text, voice_id)


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    match = CALLBACK.match(query.data or "")
    if match is None:
        await query.answer()
        return
    run_id, choice = int(match.group(1)), match.group(2)
    tg_user = update.effective_user
    user_id = await asyncio.to_thread(_ensure_user, tg_user.id, tg_user.full_name)
    original = query.message.text if query.message else ""

    if choice == "confirm":
        await query.answer("در حال انجام…")
        # Remove the buttons first so the same message cannot be tapped again.
        await query.edit_message_reply_markup(reply_markup=None)
        outcome = await asyncio.to_thread(_confirm, run_id, user_id)
        await query.edit_message_text(f"{original}\n\n{outcome.message}")
    elif choice == "cancel":
        cancelled = await asyncio.to_thread(_cancel, run_id, user_id)
        await query.answer()
        if cancelled:
            await query.edit_message_text(f"{original}\n\n{CANCELLED_TEXT}")
        else:
            await query.edit_message_reply_markup(reply_markup=None)
    else:  # edit
        status = await asyncio.to_thread(_run_status, run_id, user_id)
        await query.answer()
        if status != RunStatus.pending_confirmation:
            await query.edit_message_reply_markup(reply_markup=None)
            return
        context.user_data["edit_run_id"] = run_id
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(EDIT_PROMPT)


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

    app = Application.builder().token(settings.telegram_bot_token).concurrent_updates(True).build()
    app.add_handler(TypeHandler(Update, guard), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.VOICE, voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    app.add_handler(CallbackQueryHandler(button, pattern=r"^run:"))
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
