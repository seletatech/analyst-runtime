"""Telegram channel implementation using python-telegram-bot."""

from __future__ import annotations

import asyncio
import os
import re

import httpx
from loguru import logger
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from analyst_runtime.bus.events import OutboundMessage
from analyst_runtime.bus.queue import MessageBus
from analyst_runtime.channels.base import BaseChannel
from analyst_runtime.config.schema import TelegramConfig


def _markdown_to_telegram_html(text: str) -> str:
    """
    Convert markdown to Telegram-safe HTML.
    """
    if not text:
        return ""

    # 1. Extract and protect code blocks (preserve content from other processing)
    code_blocks: list[str] = []
    def save_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks) - 1}\x00"

    text = re.sub(r'```[\w]*\n?([\s\S]*?)```', save_code_block, text)

    # 2. Extract and protect inline code
    inline_codes: list[str] = []
    def save_inline_code(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes) - 1}\x00"

    text = re.sub(r'`([^`]+)`', save_inline_code, text)

    # 3. Headers # Title -> just the title text
    text = re.sub(r'^#{1,6}\s+(.+)$', r'\1', text, flags=re.MULTILINE)

    # 4. Blockquotes > text -> just the text (before HTML escaping)
    text = re.sub(r'^>\s*(.*)$', r'\1', text, flags=re.MULTILINE)

    # 5. Escape HTML special characters
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 6. Links [text](url) - must be before bold/italic to handle nested cases
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)

    # 7. Bold **text** or __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)

    # 8. Italic _text_ (avoid matching inside words like some_var_name)
    text = re.sub(r'(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])', r'<i>\1</i>', text)

    # 9. Strikethrough ~~text~~
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)

    # 10. Bullet lists - item -> • item
    text = re.sub(r'^[-*]\s+', '• ', text, flags=re.MULTILINE)

    # 11. Restore inline code with HTML tags
    for i, code in enumerate(inline_codes):
        # Escape HTML in code content
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00IC{i}\x00", f"<code>{escaped}</code>")

    # 12. Restore code blocks with HTML tags
    for i, code in enumerate(code_blocks):
        # Escape HTML in code content
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00CB{i}\x00", f"<pre><code>{escaped}</code></pre>")

    return text


def _split_message(content: str, max_len: int = 4000) -> list[str]:
    """Split content into chunks within max_len, preferring line breaks."""
    if len(content) <= max_len:
        return [content]
    chunks: list[str] = []
    while content:
        if len(content) <= max_len:
            chunks.append(content)
            break
        cut = content[:max_len]
        pos = cut.rfind('\n')
        if pos == -1:
            pos = cut.rfind(' ')
        if pos == -1:
            pos = max_len
        chunks.append(content[:pos])
        content = content[pos:].lstrip()
    return chunks


# Models available in /config. (display_name, model_id)
# model_id "default" means use the sandbox's configured default model.
CONFIGURABLE_MODELS = [
    ("Claude Sonnet 4.6", "default"),
    ("Kimi k2.5", "bedrock/moonshotai.kimi-k2.5"),
]


class TelegramChannel(BaseChannel):
    """
    Telegram channel using long polling.

    Simple and reliable - no webhook/public IP needed.
    """

    name = "telegram"

    # Commands registered with Telegram's command menu
    BOT_COMMANDS = [
        BotCommand("start", "Start the bot"),
        BotCommand("new", "Start a new conversation"),
        BotCommand("help", "Show available commands"),
        BotCommand("config", "Configure settings (model, etc.)"),
        BotCommand("whatsapp", "Connect or check WhatsApp status"),
        BotCommand("whatcanyoudo", "Show what I can help with"),
        BotCommand("upgrade", "Restart your sandbox onto the latest runtime"),
    ]
    _LINK_TOKEN_RE = re.compile(r"(ANALYST-RUNTIME-[A-Za-z0-9_-]+)")

    def __init__(
        self,
        config: TelegramConfig,
        bus: MessageBus,
    ):
        super().__init__(config, bus)
        self.config: TelegramConfig = config
        self._app: Application | None = None
        self._chat_ids: dict[str, int] = {}  # Map sender_id to chat_id for replies
        self._typing_tasks: dict[str, asyncio.Task] = {}  # chat_id -> typing loop task
        # Debounce state: hold rapid/split inbound messages and flush as one.
        self._pending: dict[str, list[dict]] = {}  # chat_id -> buffered message parts
        self._debounce_tasks: dict[str, asyncio.Task] = {}  # chat_id -> flush timer

    async def start(self) -> None:
        """Start the Telegram bot with long polling."""
        if not self.config.token:
            logger.error("Telegram bot token not configured")
            return

        self._running = True

        # Build the application with larger connection pool to avoid pool-timeout on long runs
        req = HTTPXRequest(connection_pool_size=16, pool_timeout=5.0, connect_timeout=30.0, read_timeout=30.0)
        builder = Application.builder().token(self.config.token).request(req).get_updates_request(req)
        if self.config.proxy:
            builder = builder.proxy(self.config.proxy).get_updates_proxy(self.config.proxy)
        self._app = builder.build()
        self._app.add_error_handler(self._on_error)

        # Add command handlers
        self._app.add_handler(CommandHandler("start", self._on_start))
        self._app.add_handler(CommandHandler("new", self._forward_command))
        self._app.add_handler(CommandHandler("help", self._forward_command))
        self._app.add_handler(CommandHandler("upgrade", self._forward_command))
        self._app.add_handler(CommandHandler("whatcanyoudo", self._forward_command))
        self._app.add_handler(CommandHandler("config", self._on_config))
        self._app.add_handler(CommandHandler("whatsapp", self._on_whatsapp))
        self._app.add_handler(CallbackQueryHandler(self._on_config_callback, pattern=r"^config:"))
        self._app.add_handler(CallbackQueryHandler(self._on_action_callback, pattern=r"^action:"))

        # Add message handler for text, photos, voice, documents
        self._app.add_handler(
            MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.VOICE | filters.AUDIO | filters.Document.ALL)
                & ~filters.COMMAND,
                self._on_message
            )
        )

        logger.info("Starting Telegram bot (polling mode)...")

        # Initialize and start polling
        await self._app.initialize()
        await self._app.start()

        # Get bot info and register command menu
        bot_info = await self._app.bot.get_me()
        logger.info(f"Telegram bot @{bot_info.username} connected")

        try:
            await self._app.bot.set_my_commands(self.BOT_COMMANDS)
            logger.debug("Telegram bot commands registered")
        except Exception as e:
            logger.warning(f"Failed to register bot commands: {e}")

        if self.config.send_only:
            logger.info("Telegram bot in send-only mode (inbound via gateway webhook)")
            # In send-only mode, inbound messages arrive via WebChannel.
            # Register a bus listener to start typing whenever a telegram message is queued.
            self.bus.add_inbound_listener(self._on_bus_inbound_typing)
            while self._running:
                await asyncio.sleep(1)
        else:
            # Start polling (this runs until stopped)
            await self._app.updater.start_polling(
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=True  # Ignore old messages on startup
            )

            # Keep running until stopped
            while self._running:
                await asyncio.sleep(1)

    async def stop(self) -> None:
        """Stop the Telegram bot."""
        self._running = False

        # Cancel all debounce timers and typing indicators
        for task in self._debounce_tasks.values():
            if not task.done():
                task.cancel()
        self._debounce_tasks.clear()
        self._pending.clear()
        for chat_id in list(self._typing_tasks):
            self._stop_typing(chat_id)

        if self._app:
            logger.info("Stopping Telegram bot...")
            if not self.config.send_only:
                await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            self._app = None

    @staticmethod
    def _get_media_type(path: str) -> str:
        """Guess media type from file extension."""
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in ("jpg", "jpeg", "png", "gif", "webp"):
            return "photo"
        if ext == "ogg":
            return "voice"
        if ext in ("mp3", "m4a", "wav", "aac"):
            return "audio"
        return "document"

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Telegram."""
        if not self._app:
            logger.warning("Telegram bot not running")
            return

        self._stop_typing(msg.chat_id)

        try:
            chat_id = int(msg.chat_id)
        except ValueError:
            logger.error(f"Invalid chat_id: {msg.chat_id}")
            return

        # Send media files
        for media_path in (msg.media or []):
            try:
                media_type = self._get_media_type(media_path)
                sender = {
                    "photo": self._app.bot.send_photo,
                    "voice": self._app.bot.send_voice,
                    "audio": self._app.bot.send_audio,
                }.get(media_type, self._app.bot.send_document)
                param = "photo" if media_type == "photo" else media_type if media_type in ("voice", "audio") else "document"
                with open(media_path, 'rb') as f:
                    await sender(chat_id=chat_id, **{param: f})
            except Exception as e:
                filename = media_path.rsplit("/", 1)[-1]
                logger.error(f"Failed to send media {media_path}: {e}")
                await self._app.bot.send_message(chat_id=chat_id, text=f"[Failed to send: {filename}]")

        # Send text content
        if msg.content and msg.content != "[empty message]":
            chunks = _split_message(msg.content)
            # Inline keyboard only goes on the last chunk
            keyboard_data = (msg.metadata or {}).get("inline_keyboard")
            reply_markup = InlineKeyboardMarkup(keyboard_data) if keyboard_data else None
            for i, chunk in enumerate(chunks):
                markup = reply_markup if (i == len(chunks) - 1) else None
                try:
                    html = _markdown_to_telegram_html(chunk)
                    await self._app.bot.send_message(
                        chat_id=chat_id, text=html, parse_mode="HTML", reply_markup=markup
                    )
                except Exception as e:
                    logger.warning(f"HTML parse failed, falling back to plain text: {e}")
                    try:
                        await self._app.bot.send_message(
                            chat_id=chat_id, text=chunk, reply_markup=markup
                        )
                    except Exception as e2:
                        logger.error(f"Error sending Telegram message: {e2}")

    async def _on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        if not update.message or not update.effective_user:
            return

        user = update.effective_user
        chat_id = str(update.message.chat_id)

        # Read deep-link attribution payload, e.g. t.me/Bot?start=tw_en_story1
        attribution = context.args[0] if context.args else None
        if attribution:
            logger.info(
                "new_user_attribution source=%s chat_id=%s user_id=%s",
                attribution, chat_id, user.id,
            )

        is_authorized = await self._is_sender_authorized("telegram", chat_id)
        if is_authorized:
            await update.message.reply_text(
                f"👋 Hi {user.first_name}! I'm Samantha.\n\n"
                "Send me a message and I'll respond!\n"
                "Type /help to see available commands."
            )
        else:
            await update.message.reply_text(
                f"👋 Hi {user.first_name}!\n\n"
                "To get started, sign up at talktosamantha.co and link your Telegram account."
            )


    async def _on_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /config command — show model selection keyboard."""
        if not update.message:
            return
        keyboard = [
            [InlineKeyboardButton(name, callback_data=f"config:model:{mid}")]
            for name, mid in CONFIGURABLE_MODELS
        ]
        await update.message.reply_text(
            "⚙️ <b>Configuration</b>\n\nSelect default model for this chat:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

    async def _on_config_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline-keyboard model selection from /config."""
        query = update.callback_query
        if not query or not query.data or not query.message:
            return
        await query.answer()

        parts = query.data.split(":", 2)
        if len(parts) != 3 or parts[0] != "config" or parts[1] != "model":
            return
        model_id = parts[2]

        display_name = next(
            (name for name, mid in CONFIGURABLE_MODELS if mid == model_id),
            model_id,
        )

        chat_id = str(query.message.chat_id)
        sender_id = self._sender_id(query.from_user) if query.from_user else chat_id

        # Forward config change to AgentLoop via the bus as an internal command.
        # AgentLoop stores it in session.metadata and returns None (no extra reply).
        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=f"__config:model:{model_id}",
        )

        await query.edit_message_text(f"✅ Model set to: <b>{display_name}</b>", parse_mode="HTML")

    async def _on_action_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle action chip button taps — forward the action text to the agent as a user message."""
        query = update.callback_query
        if not query or not query.data or not query.message:
            return
        await query.answer()

        action_text = query.data[len("action:"):]
        if not action_text:
            return

        chat_id = str(query.message.chat_id)
        sender_id = self._sender_id(query.from_user) if query.from_user else chat_id

        logger.info("Action chip tapped: %r from chat_id=%s", action_text[:60], chat_id)

        # Remove the keyboard from the triggering message so it can't be tapped twice
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        self._start_typing(chat_id)
        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=action_text,
        )

    @staticmethod
    def _sender_id(user) -> str:
        """Build sender_id with username for allowlist matching."""
        sid = str(user.id)
        return f"{sid}|{user.username}" if user.username else sid

    async def _on_whatsapp(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /whatsapp — show WhatsApp bridge connection status."""
        if not update.message:
            return

        import json as _json

        from analyst_runtime.utils.helpers import get_data_path

        status_path = get_data_path() / "whatsapp" / "status.json"
        try:
            state = _json.loads(status_path.read_text()).get("state", "disconnected") if status_path.exists() else "disconnected"
        except Exception:
            state = "disconnected"

        if state == "connected":
            text = "✅ <b>WhatsApp connected.</b>\n\nYou can message Samantha on WhatsApp."
        elif state == "qr_pending":
            text = (
                "⏳ <b>Waiting for QR scan.</b>\n\n"
                "Check the QR image I sent above and scan it in WhatsApp:\n"
                "<b>Settings → Linked Devices → Link a Device</b>"
            )
        else:
            text = (
                "📵 <b>WhatsApp bridge is not running.</b>\n\n"
                "Start the bridge on the server:\n"
                "<code>analyst_runtime channels login</code>\n\n"
                "I'll send you a QR code automatically once it's ready to scan."
            )

        await update.message.reply_text(text, parse_mode="HTML")

    async def _forward_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Forward slash commands to the bus for unified handling in AgentLoop."""
        if not update.message or not update.effective_user:
            return
        await self._handle_message(
            sender_id=self._sender_id(update.effective_user),
            chat_id=str(update.message.chat_id),
            content=update.message.text,
        )

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming messages (text, photos, voice, documents)."""
        if not update.message or not update.effective_user:
            return

        message = update.message
        user = update.effective_user
        chat_id = message.chat_id
        sender_id = self._sender_id(user)

        # Store chat_id for replies
        self._chat_ids[sender_id] = chat_id

        # Build content from text and/or media
        content_parts = []
        media_paths = []

        # Text content
        if message.text:
            content_parts.append(message.text)
        if message.caption:
            content_parts.append(message.caption)

        # Handle media files
        media_file = None
        media_type = None

        if message.photo:
            media_file = message.photo[-1]  # Largest photo
            media_type = "image"
        elif message.voice:
            media_file = message.voice
            media_type = "voice"
        elif message.audio:
            media_file = message.audio
            media_type = "audio"
        elif message.document:
            media_file = message.document
            media_type = "file"

        # Download media if present
        if media_file and self._app:
            try:
                file = await self._app.bot.get_file(media_file.file_id)
                ext = self._get_extension(media_type, getattr(media_file, 'mime_type', None))

                from analyst_runtime.utils.helpers import get_data_path
                media_dir = get_data_path() / "media"
                media_dir.mkdir(parents=True, exist_ok=True)

                file_path = media_dir / f"{media_file.file_id[:16]}{ext}"
                await file.download_to_drive(str(file_path))

                media_paths.append(str(file_path))

                # Handle voice transcription via gateway (Amazon Transcribe)
                if media_type == "voice" or media_type == "audio":
                    transcription = await self._transcribe_via_gateway(str(file_path))
                    if transcription:
                        logger.info(f"Transcribed {media_type}: {transcription[:50]}...")
                        content_parts.append(f"[transcription: {transcription}]")
                    else:
                        content_parts.append(f"[{media_type}: {file_path}]")
                else:
                    content_parts.append(f"[{media_type}: {file_path}]")

                logger.debug(f"Downloaded {media_type} to {file_path}")
            except Exception as e:
                logger.error(f"Failed to download media: {e}")
                content_parts.append(f"[{media_type}: download failed]")

        content = "\n".join(content_parts) if content_parts else "[empty message]"

        logger.debug(f"Telegram message from {sender_id}: {content[:50]}...")

        str_chat_id = str(chat_id)
        link_token = self._extract_link_token(content)

        # Allow first-contact binding via one-time token sent in Telegram chat.
        if link_token:
            bound = await self._bind_sender_via_link_token(
                token=link_token,
                external_id=str_chat_id,
                display_name=user.first_name or user.username or sender_id,
            )
            if bound:
                await message.reply_text(
                    "✅ Telegram linked successfully.\n"
                    "You can now chat with me here."
                )
            else:
                await message.reply_text(
                    "❌ Link token is invalid, expired, or already used.\n"
                    "Generate a new token from Aura-Web and send it again."
                )
            return

        # Authorization check: only process if sender is bound to this sandbox's owner
        if not await self._is_sender_authorized("telegram", str_chat_id):
            logger.debug(f"Ignoring unauthorized Telegram message from chat_id={str_chat_id}")
            await message.reply_text(
                "To chat with Samantha, sign up at talktosamantha.co and link your Telegram account."
            )
            return

        self._ensure_allow_from(str_chat_id)

        # Start typing immediately so the user sees a response indicator right away.
        self._start_typing(str_chat_id)

        # Buffer this message and (re)start the debounce timer.  The timer flushes
        # all buffered parts as a single combined message once the user goes idle for
        # DEBOUNCE_SECS.  This handles both rapid multi-message bursts and long
        # messages that Telegram splits at the 4096-char limit.
        self._pending.setdefault(str_chat_id, []).append({
            "content": content,
            "media": media_paths,
            "sender_id": sender_id,
            "metadata": {
                "message_id": message.message_id,
                "user_id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "is_group": message.chat.type != "private",
            },
        })
        self._reset_debounce(str_chat_id)

    # Idle window before a buffered burst is flushed as a single agent turn.
    DEBOUNCE_SECS: float = 0.8

    def _reset_debounce(self, chat_id: str) -> None:
        """Cancel any pending flush timer for chat_id and start a fresh one."""
        existing = self._debounce_tasks.pop(chat_id, None)
        if existing and not existing.done():
            existing.cancel()
        self._debounce_tasks[chat_id] = asyncio.create_task(
            self._flush_after_delay(chat_id)
        )

    async def _flush_after_delay(self, chat_id: str) -> None:
        """Wait DEBOUNCE_SECS then flush all buffered messages for chat_id."""
        try:
            await asyncio.sleep(self.DEBOUNCE_SECS)
        except asyncio.CancelledError:
            return
        await self._flush_pending(chat_id)

    async def _flush_pending(self, chat_id: str) -> None:
        """Combine buffered message parts and publish a single inbound event."""
        self._debounce_tasks.pop(chat_id, None)
        parts = self._pending.pop(chat_id, [])
        if not parts:
            return

        # Combine text content; skip bare "[empty message]" placeholders unless
        # every part is empty (i.e. the whole burst was media-only).
        text_parts = [p["content"] for p in parts if p["content"] != "[empty message]"]
        combined_content = "\n".join(text_parts) if text_parts else "[empty message]"

        combined_media: list[str] = []
        for p in parts:
            combined_media.extend(p.get("media") or [])

        # Use the last part's sender/metadata (most recent message in the burst).
        sender_id = parts[-1]["sender_id"]
        metadata = parts[-1]["metadata"]

        if len(parts) > 1:
            logger.debug(
                "Debounce flush: combined %d messages for chat_id=%s", len(parts), chat_id
            )

        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=combined_content,
            media=combined_media or None,
            metadata=metadata,
        )

    @classmethod
    def _extract_link_token(cls, content: str) -> str | None:
        """Extract a ANALYST_RUNTIME one-time link token from free-form text."""
        if not content:
            return None
        match = cls._LINK_TOKEN_RE.search(content.strip())
        return match.group(1) if match else None

    def _ensure_allow_from(self, external_id: str) -> None:
        """Keep runtime allowlist in sync for newly authorized senders."""
        allow_list = getattr(self.config, "allow_from", None)
        if isinstance(allow_list, list) and allow_list and external_id not in allow_list:
            allow_list.append(external_id)

    async def _bind_sender_via_link_token(
        self,
        *,
        token: str,
        external_id: str,
        display_name: str,
    ) -> bool:
        """Bind sender by exchanging a one-time token with the gateway."""
        gateway_url = os.environ.get("GATEWAY_URL", "").rstrip("/")
        if not gateway_url:
            logger.warning("Cannot bind Telegram sender: GATEWAY_URL is not set")
            return False

        payload = {
            "token": token,
            "external_id": external_id,
            "display_name": display_name,
        }
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{gateway_url}/api/channels/bind",
                    json=payload,
                    timeout=5.0,
                )
            if resp.status_code == 200:
                logger.info("Telegram link token bind success for chat_id=%s", external_id)
                self._ensure_allow_from(external_id)
                return True
            logger.warning(
                "Telegram link token bind failed for chat_id=%s status=%s",
                external_id, resp.status_code,
            )
            return False
        except Exception as exc:
            logger.warning("Telegram link token bind request failed: %s", exc)
            return False

    async def _transcribe_via_gateway(self, file_path: str) -> str | None:
        """Transcribe audio via the gateway /tools/transcribe endpoint (Amazon Transcribe)."""
        from analyst_runtime.agent.tools.gateway_auth import gateway_client
        try:
            async with gateway_client(timeout=90.0) as client:
                resp = await client.post("/tools/transcribe", json={"file_path": file_path})
                if resp.status_code == 200:
                    return resp.json().get("transcript") or None
        except Exception as exc:
            logger.warning("Gateway transcription failed: %s", exc)
        return None

    async def _on_bus_inbound_typing(self, msg) -> None:
        """Bus inbound listener: start typing when a telegram message is queued."""
        if getattr(msg, "channel", None) == "telegram" and msg.chat_id:
            self._start_typing(msg.chat_id)

    def _start_typing(self, chat_id: str) -> None:
        """Start sending 'typing...' indicator for a chat."""
        # Cancel any existing typing task for this chat
        self._stop_typing(chat_id)
        self._typing_tasks[chat_id] = asyncio.create_task(self._typing_loop(chat_id))

    def _stop_typing(self, chat_id: str) -> None:
        """Stop the typing indicator for a chat."""
        task = self._typing_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    async def _typing_loop(self, chat_id: str) -> None:
        """Repeatedly send 'typing' action until cancelled."""
        try:
            while self._app:
                await self._app.bot.send_chat_action(chat_id=int(chat_id), action="typing")
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"Typing indicator stopped for {chat_id}: {e}")

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log polling / handler errors instead of silently swallowing them."""
        logger.error(f"Telegram error: {context.error}")

    def _get_extension(self, media_type: str, mime_type: str | None) -> str:
        """Get file extension based on media type."""
        if mime_type:
            ext_map = {
                "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a",
            }
            if mime_type in ext_map:
                return ext_map[mime_type]

        type_map = {"image": ".jpg", "voice": ".ogg", "audio": ".mp3", "file": ""}
        return type_map.get(media_type, "")
