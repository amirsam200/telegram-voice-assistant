"""Telegram bot (long polling). Only allowlisted users may use it.

Voice (or text) -> transcript -> agent -> Persian preview with Confirm / Edit / Cancel.
Only Confirm runs the action, and a run executes at most once.
"""

import asyncio
import logging
import re
from functools import lru_cache
from math import ceil
from pathlib import Path

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, NetworkError
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

from app.agent import contacts, fa
from app.agent.actions import AddContactAction
from app.agent.agent import run_agent
from app.agent.executor import cancel, confirm_and_execute, preview
from app.agent.provider import DeepSeekProvider, LLMError
from app.bot.limits import RateLimiter, delete_old_audio, retry_async
from app.config import get_settings
from app.db.models import AgentRun, AuditEvent, RunStatus, User, VoiceMessage
from app.db.session import SessionLocal, session_scope
from app.logging_setup import configure_logging, set_run_id
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
    "• مخاطب: «رضا احمدی را با ایمیل reza@example.com به مخاطبان اضافه کن».\n"
    "• پیام متنی هم مثل پیام صوتی پردازش می‌شود.\n"
    "پیش از انجام هر کاری پیش‌نمایش آن را می‌بینید و با «تأیید» اجرا می‌شود.\n"
    "• /addcontact نام ایمیل  افزودن مخاطب\n"
    "• /contacts  فهرست مخاطبان\n"
    "• /delcontact نام  حذف مخاطب\n"
    "• /start شروع\n"
    "• /help همین راهنما"
)

OWNER_ONLY_TEXT = "مدیریت مخاطبان فقط برای مالک ربات مجاز است."

ERROR_TEXT = "متأسفم، خطایی رخ داد. لطفاً دوباره تلاش کنید."
REPEAT_TEXT = "متأسفم، صدایتان را واضح متوجه نشدم. لطفاً آرام‌تر و شمرده‌تر دوباره بگویید."
LLM_DOWN_TEXT = "سرویس هوش مصنوعی فعلاً در دسترس نیست. لطفاً چند دقیقه بعد دوباره تلاش کنید."
STT_FAILED_TEXT = "متأسفم، تبدیل صدا به متن ناموفق بود. لطفاً دوباره بفرستید یا بنویسید."
EDIT_PROMPT = "✏️ چه تغییری بدهم؟ با پیام صوتی یا متنی بگویید."
CANCELLED_TEXT = "❌ لغو شد. کاری انجام نشد."
TOO_LONG_TEXT = "پیام صوتی حداکثر {} ثانیه می‌تواند باشد. لطفاً کوتاه‌تر بفرستید."
RATE_LIMITED_TEXT = "پیام‌های شما زیاد است. لطفاً {} ثانیه دیگر دوباره بفرستید."
DOWNLOAD_FAILED_TEXT = "متأسفم، دریافت پیام صوتی از تلگرام ناموفق بود. لطفاً دوباره بفرستید."

DOWNLOAD_ATTEMPTS = 3

CALLBACK = re.compile(r"^run:(\d+):(confirm|edit|cancel)$")


@lru_cache
def _provider() -> DeepSeekProvider:
    return DeepSeekProvider()


@lru_cache
def _limiter() -> RateLimiter:
    return RateLimiter(get_settings().rate_limit_per_minute, window_s=60)


def _keyboard(run_id: int, editable: bool = True) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton("✅ تأیید", callback_data=f"run:{run_id}:confirm")]
    if editable:
        buttons.append(InlineKeyboardButton("✏️ ویرایش", callback_data=f"run:{run_id}:edit"))
    buttons.append(InlineKeyboardButton("❌ لغو", callback_data=f"run:{run_id}:cancel"))
    return InlineKeyboardMarkup([buttons])


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every handler; stops updates from users not on the allowlist."""
    user = update.effective_user
    if user is not None and user.id in get_settings().allowed_ids:
        # Button taps are not counted: they only act on proposals the user already has.
        if update.callback_query is None and update.effective_message is not None:
            wait_s = _limiter().hit(user.id)
            if wait_s is not None:
                logger.warning("Rate limited user %s for %.0f s", user.id, wait_s)
                await update.effective_message.reply_text(RATE_LIMITED_TEXT.format(fa.to_fa_digits(ceil(wait_s))))
                raise ApplicationHandlerStop
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
        if isinstance(result.action, AddContactAction) and user.telegram_user_id != get_settings().owner_telegram_id:
            run = session.get(AgentRun, result.run_id)
            run.status = RunStatus.rejected
            run.reply_text = OWNER_ONLY_TEXT
            session.commit()
            return result.run_id, OWNER_ONLY_TEXT, False
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


def _propose_add(user_id: int, args: list[str], input_text: str) -> contacts.Proposal:
    with session_scope() as session:
        return contacts.propose(session, user_id, contacts.parse_add(args), input_text)


def _propose_delete(user_id: int, args: list[str], input_text: str) -> contacts.Proposal:
    with session_scope() as session:
        return contacts.propose(session, user_id, contacts.resolve_delete(session, user_id, args), input_text)


def _contacts_text(user_id: int) -> str:
    with session_scope() as session:
        return contacts.list_text(session, user_id)


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
        set_run_id(run_id)
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


async def _download_voice(tg_voice, target: Path, sleep=asyncio.sleep) -> bool:
    """Download with 3 attempts and exponential backoff (1 s, 2 s). False if every attempt failed."""
    async def attempt() -> None:
        tg_file = await tg_voice.get_file()
        await tg_file.download_to_drive(custom_path=target)

    try:
        # NetworkError covers TimedOut; BadRequest (also a NetworkError) will not improve on retry.
        await retry_async(attempt, retry_on=(NetworkError,), give_up_on=(BadRequest,),
                          attempts=DOWNLOAD_ATTEMPTS, sleep=sleep, what="Voice download")
    except NetworkError:
        target.unlink(missing_ok=True)
        return False
    return True


async def voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    tg_user = update.effective_user
    tg_voice = message.voice
    max_s = get_settings().max_voice_seconds
    if (tg_voice.duration or 0) > max_s:
        logger.info("Refused a %ss voice message (limit %ss)", tg_voice.duration, max_s)
        await message.reply_text(TOO_LONG_TEXT.format(fa.to_fa_digits(max_s)))
        return
    user_id = await asyncio.to_thread(_ensure_user, tg_user.id, tg_user.full_name)

    target_dir = Path(get_settings().voice_dir) / str(tg_user.id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{message.message_id}_{tg_voice.file_unique_id}.ogg"
    if not await _download_voice(tg_voice, target):
        await message.reply_text(DOWNLOAD_FAILED_TEXT)
        return

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


async def _owner_user_id(update: Update) -> int | None:
    """The owner's users.id, or None (after telling the user) for anyone else on the allowlist."""
    tg_user = update.effective_user
    if tg_user.id != get_settings().owner_telegram_id:
        await update.effective_message.reply_text(OWNER_ONLY_TEXT)
        return None
    return await asyncio.to_thread(_ensure_user, tg_user.id, tg_user.full_name)


async def _propose_command(update: Update, context: ContextTypes.DEFAULT_TYPE, build) -> None:
    user_id = await _owner_user_id(update)
    if user_id is None:
        return
    message = update.effective_message
    try:
        proposal = await asyncio.to_thread(build, user_id, list(context.args or []), message.text or "")
    except contacts.CommandError as exc:
        await message.reply_text(str(exc))
        return
    await message.reply_text(proposal.preview, reply_markup=_keyboard(proposal.run_id, editable=False))


async def add_contact_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _propose_command(update, context, _propose_add)


async def delete_contact_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _propose_command(update, context, _propose_delete)


async def contacts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = await _owner_user_id(update)
    if user_id is not None:
        await update.effective_message.reply_text(await asyncio.to_thread(_contacts_text, user_id))


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    match = CALLBACK.match(query.data or "")
    if match is None:
        await query.answer()
        return
    run_id, choice = int(match.group(1)), match.group(2)
    set_run_id(run_id)  # copied into the worker threads below
    logger.info("Button %s pressed", choice)
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


async def _cleanup_loop() -> None:
    """Every few minutes, delete audio left behind (e.g. by a crash) for longer than an hour."""
    s = get_settings()
    while True:
        try:
            deleted = await asyncio.to_thread(delete_old_audio, s.voice_dir, s.audio_max_age_s)
            if deleted:
                logger.info("Deleted %d leftover audio file(s)", deleted)
        except Exception:
            logger.exception("Audio cleanup failed")
        await asyncio.sleep(s.audio_cleanup_interval_s)


async def _start_cleanup(app: Application) -> None:
    app.bot_data["cleanup_task"] = asyncio.create_task(_cleanup_loop())


async def _stop_cleanup(app: Application) -> None:
    task = app.bot_data.pop("cleanup_task", None)
    if task is not None:
        task.cancel()


def build_application() -> Application:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    if not settings.allowed_ids:
        logger.warning("ALLOWED_USER_IDS is empty; every user will be refused")

    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(True)
        .post_init(_start_cleanup)
        .post_shutdown(_stop_cleanup)
        .build()
    )
    app.add_handler(TypeHandler(Update, guard), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("addcontact", add_contact_command))
    app.add_handler(CommandHandler("contacts", contacts_command))
    app.add_handler(CommandHandler("delcontact", delete_contact_command))
    app.add_handler(MessageHandler(filters.VOICE, voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    app.add_handler(CallbackQueryHandler(button, pattern=r"^run:"))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    # JSON lines; httpx stays at WARNING because its request URLs contain the bot token.
    configure_logging(logging.DEBUG if get_settings().debug else logging.INFO)
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)
