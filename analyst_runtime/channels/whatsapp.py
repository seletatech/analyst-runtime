"""WhatsApp channel implementation using Node.js bridge."""

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from loguru import logger

from analyst_runtime.bus.events import InboundMessage, OutboundMessage
from analyst_runtime.bus.queue import MessageBus
from analyst_runtime.channels.base import BaseChannel
from analyst_runtime.config.schema import WhatsAppConfig
from analyst_runtime.utils.helpers import get_data_path


class WhatsAppChannel(BaseChannel):
    """
    WhatsApp channel that connects to a Node.js bridge.

    The bridge uses @whiskeysockets/baileys to handle the WhatsApp Web protocol.
    Communication between Python and Node.js is via WebSocket.
    """

    name = "whatsapp"
    _LINK_TOKEN_RE = re.compile(r"(ANALYST-RUNTIME-[A-Za-z0-9_-]+)")

    def __init__(
        self,
        config: WhatsAppConfig,
        bus: MessageBus,
        owner_chat_id: str | None = None,
    ):
        super().__init__(config, bus)
        self.config: WhatsAppConfig = config
        self._ws = None
        self._connected = False
        # Telegram chat_id of the sandbox owner — used to push QR codes and
        # connection status notifications proactively.
        self._owner_chat_id = owner_chat_id
        self._bridge_proc: asyncio.subprocess.Process | None = None
        self._last_qr_time: datetime | None = None
        self._own_jid: str | None = None  # set on connect; drives self-only mode

    async def start(self) -> None:
        """Start the WhatsApp channel by connecting to the bridge."""
        import websockets

        bridge_url = self.config.bridge_url

        logger.info(f"Connecting to WhatsApp bridge at {bridge_url}...")

        self._running = True

        while self._running:
            try:
                async with websockets.connect(bridge_url) as ws:
                    self._ws = ws
                    # Send auth token if configured
                    if self.config.bridge_token:
                        await ws.send(json.dumps({"type": "auth", "token": self.config.bridge_token}))
                    self._connected = True
                    logger.info("Connected to WhatsApp bridge")

                    # Restore own_jid from persisted status as a fallback — the
                    # bridge will replay 'status: connected' if WhatsApp is already
                    # up, but if the file has it we can use it right away.
                    if self._own_jid is None:
                        try:
                            status_data = json.loads((self._wa_dir() / "status.json").read_text())
                            if status_data.get("own_jid"):
                                self._own_jid = status_data["own_jid"]
                                logger.info("Restored own_jid from status.json: %s", self._own_jid)
                        except Exception:
                            pass

                    # Listen for messages
                    async for message in ws:
                        try:
                            await self._handle_bridge_message(message)
                        except Exception as e:
                            logger.error(f"Error handling bridge message: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                self._ws = None
                logger.warning(f"WhatsApp bridge connection error: {e}")

                if self._running:
                    logger.info("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)

    async def stop(self) -> None:
        """Stop the WhatsApp channel."""
        self._running = False
        self._connected = False

        if self._ws:
            await self._ws.close()
            self._ws = None

    async def start_bridge(self) -> bool:
        """Spawn the Node.js bridge in the background.

        Returns True if the bridge was started or is already running.
        The existing WebSocket reconnect loop will connect automatically
        once the bridge is up (within ~5 seconds).
        """
        if self._bridge_proc is not None and self._bridge_proc.returncode is None:
            return True  # already running

        bridge_dir: Path | None = None
        for candidate in [Path.home() / ".analyst-runtime" / "bridge", Path("/app/bridge")]:
            if (candidate / "dist" / "index.js").exists():
                bridge_dir = candidate
                break

        if bridge_dir is None:
            logger.warning("WhatsApp bridge not found (no dist/index.js in known locations)")
            return False

        env = {**os.environ}
        if self.config.bridge_token:
            env["BRIDGE_TOKEN"] = self.config.bridge_token

        self._bridge_proc = await asyncio.create_subprocess_exec(
            "npm", "start",
            cwd=str(bridge_dir),
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        logger.info("WhatsApp bridge started (pid=%d)", self._bridge_proc.pid)
        return True

    # ------------------------------------------------------------------
    # Binding helpers
    # ------------------------------------------------------------------

    @classmethod
    def _extract_link_token(cls, content: str) -> str | None:
        """Extract a ANALYST_RUNTIME one-time link token from free-form text."""
        if not content:
            return None
        match = cls._LINK_TOKEN_RE.search(content.strip())
        return match.group(1) if match else None

    def _ensure_allow_from(self, sender_id: str) -> None:
        """Keep in-memory allowlist in sync for newly authorized senders."""
        allow_list = getattr(self.config, "allow_from", None)
        if isinstance(allow_list, list) and sender_id not in allow_list:
            allow_list.append(sender_id)

    async def _bind_sender_via_link_token(
        self,
        *,
        token: str,
        external_id: str,
        display_name: str,
    ) -> bool:
        """Exchange a one-time link token with the gateway to bind this WhatsApp ID."""
        gateway_url = os.environ.get("GATEWAY_URL", "").rstrip("/")
        if not gateway_url:
            logger.warning("Cannot bind WhatsApp sender: GATEWAY_URL is not set")
            return False
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{gateway_url}/api/channels/bind",
                    json={"token": token, "external_id": external_id, "display_name": display_name},
                    timeout=5.0,
                )
            if resp.status_code == 200:
                logger.info("WhatsApp link token bind success for %s", external_id)
                return True
            logger.warning(
                "WhatsApp link token bind failed for %s status=%s",
                external_id, resp.status_code,
            )
            return False
        except Exception as exc:
            logger.warning("WhatsApp link token bind request failed: %s", exc)
            return False

    async def _reply_to_sender(self, chat_id: str, text: str) -> None:
        """Send a plain-text reply directly to a WhatsApp sender via the bridge."""
        if not self._ws or not self._connected:
            return
        try:
            await self._ws.send(json.dumps({"type": "send", "to": chat_id, "text": text}))
        except Exception as exc:
            logger.warning("WhatsApp reply to %s failed: %s", chat_id, exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _wa_dir(self) -> Path:
        d = get_data_path() / "whatsapp"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write_status(self, state: str) -> None:
        """Persist connection state so other channels (Telegram) can query it."""
        try:
            entry: dict = {"state": state, "ts": datetime.now(timezone.utc).isoformat()}
            if self._own_jid:
                entry["own_jid"] = self._own_jid
            (self._wa_dir() / "status.json").write_text(json.dumps(entry))
        except Exception as e:
            logger.warning(f"WhatsApp status write failed: {e}")

    def _log_message(self, role: str, text: str, chat_id: str = "") -> None:
        """Append a message entry to the persistent message log."""
        try:
            entry = json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "role": role,
                "text": text,
                "chat_id": chat_id,
            })
            with open(self._wa_dir() / "message_log.jsonl", "a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except Exception as e:
            logger.warning(f"WhatsApp message log write failed: {e}")

    def _generate_qr_image(self, qr_data: str) -> str | None:
        """Render QR string to a PNG file and return its path."""
        try:
            import segno
            qr_path = self._wa_dir() / "qr.png"
            segno.make_qr(qr_data).save(str(qr_path), scale=8, border=2)
            return str(qr_path)
        except Exception as e:
            logger.warning(f"QR image generation failed: {e}")
            return None

    async def _notify_owner(self, text: str, media: list[str] | None = None) -> None:
        """Push a Telegram message to the sandbox owner via the outbound bus."""
        if not self._owner_chat_id:
            return
        await self.bus.publish_outbound(OutboundMessage(
            channel="telegram",
            chat_id=self._owner_chat_id,
            content=text,
            media=media or [],
        ))

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through WhatsApp."""
        if not self._ws or not self._connected:
            logger.warning("WhatsApp bridge not connected")
            return

        try:
            payload = {"type": "send", "to": msg.chat_id, "text": msg.content}
            await self._ws.send(json.dumps(payload))
            self._log_message("assistant", msg.content, msg.chat_id)
        except Exception as e:
            logger.error(f"Error sending WhatsApp message: {e}")

    # ------------------------------------------------------------------
    # Bridge message handler
    # ------------------------------------------------------------------

    async def _handle_bridge_message(self, raw: str) -> None:
        """Handle a message from the bridge."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON from bridge: {raw[:100]}")
            return

        msg_type = data.get("type")

        if msg_type == "message":
            # Incoming message from WhatsApp
            # Deprecated by whatsapp: old phone number style typically: <phone>@s.whatsapp.net
            pn = data.get("pn", "")
            # New LID style typically:
            sender = data.get("sender", "")
            content = data.get("content", "")

            # WhatsApp sends two JID formats per message:
            #   sender (remoteJid)    — phone-number format: 447394414812@s.whatsapp.net
            #   pn     (remoteJidAlt) — LID format:          40441579843636@lid
            # Use the LID as the primary sender_id when present (modern WhatsApp),
            # but keep the phone number separately for self-only comparison, because
            # own_jid is always phone-format.
            user_id = pn if pn else sender
            sender_id = user_id.split("@")[0] if "@" in user_id else user_id
            sender_phone = sender.split("@")[0] if "@" in sender else sender_id
            logger.info(f"WA msg: sender={sender!r} pn={pn!r} sender_id={sender_id!r} sender_phone={sender_phone!r}")

            # Silently ignore group messages — the bridge is a linked device and
            # sees all incoming traffic; we must never auto-reply into groups.
            if data.get("isGroup", False):
                logger.debug("Ignoring group message from %s", sender_id)
                return

            # Handle voice transcription if it's a voice message
            if content == "[Voice Message]":
                logger.info(f"Voice message received from {sender_id}, but direct download from bridge is not yet supported.")
                content = "[Voice Message: Transcription not available for WhatsApp yet]"

            allow_list = getattr(self.config, "allow_from", [])

            # Self-only mode: no allow_from configured → only accept messages
            # from the linked account's own number (user messaging themselves).
            # No link token or gateway auth needed.
            if not allow_list:
                if not self._own_jid:
                    logger.info("Self-only: own JID unknown yet, dropping message from %s", sender_id)
                    return
                own_phone = self._own_jid.split(":")[0].split("@")[0]
                logger.info(f"WA self-only check: own_jid={self._own_jid!r} own_phone={own_phone!r} sender_phone={sender_phone!r}")
                if sender_phone != own_phone:
                    logger.info(f"Self-only: dropping message from {sender_id!r} (own: {own_phone!r})")
                    return
                # Own phone confirmed — publish directly.
                # Do NOT call _ensure_allow_from here: it would add the sender LID to
                # allow_from, converting self-only mode to allow_from mode and causing
                # every subsequent message to fail gateway authorization.
                self._log_message("user", content, sender)
                await self.bus.publish_inbound(InboundMessage(
                    channel=self.name,
                    sender_id=str(sender_id),
                    chat_id=str(sender),
                    content=content,
                    metadata={
                        "message_id": data.get("id"),
                        "timestamp": data.get("timestamp"),
                        "is_group": data.get("isGroup", False),
                    },
                ))
                return

            # allow_from configured: support link-token binding for external numbers.
            link_token = self._extract_link_token(content)
            if link_token:
                bound = await self._bind_sender_via_link_token(
                    token=link_token,
                    external_id=sender,
                    display_name=sender_id,
                )
                if bound:
                    self._ensure_allow_from(sender_id)
                    await self._reply_to_sender(
                        sender,
                        "✅ WhatsApp linked to your account. You can now chat with me here.",
                    )
                else:
                    await self._reply_to_sender(
                        sender,
                        "❌ Link token is invalid, expired, or already used. "
                        "Generate a new token from the app and send it again.",
                    )
                return

            if not await self._is_sender_authorized("whatsapp", sender):
                logger.debug("Ignoring unauthorized WhatsApp message from %s", sender_id)
                return

            self._ensure_allow_from(sender_id)

            self._log_message("user", content, sender)
            await self._handle_message(
                sender_id=sender_id,
                chat_id=sender,  # Use full LID for replies
                content=content,
                metadata={
                    "message_id": data.get("id"),
                    "timestamp": data.get("timestamp"),
                    "is_group": data.get("isGroup", False),
                },
            )

        elif msg_type == "status":
            status = data.get("status")
            logger.info(f"WhatsApp status: {status}")

            if status == "connected":
                self._connected = True
                self._own_jid = data.get("ownJid") or self._own_jid
                self._write_status("connected")
                logger.info("WhatsApp connected (ownJid=%s)", self._own_jid)
                await self._notify_owner("✅ WhatsApp connected! You can now message Samantha on WhatsApp.")

            elif status == "disconnected":
                self._connected = False
                self._write_status("disconnected")

        elif msg_type == "qr":
            qr_string = data.get("qr", "")
            now = datetime.now(timezone.utc)
            if self._last_qr_time and (now - self._last_qr_time).total_seconds() < 5:
                logger.debug("WhatsApp QR debounced (duplicate event within 5s)")
                return
            self._last_qr_time = now
            logger.info("WhatsApp QR received — forwarding to owner via Telegram")
            self._write_status("qr_pending")

            if self._owner_chat_id and qr_string:
                qr_path = self._generate_qr_image(qr_string)
                if qr_path:
                    await self._notify_owner(
                        "📱 Scan this QR code with WhatsApp to connect:\n"
                        "Settings → Linked Devices → Link a Device",
                        media=[qr_path],
                    )
                else:
                    # Fallback: send raw QR string (can't generate image)
                    await self._notify_owner(
                        f"📱 Open https://wa.me/qr/ and enter this code to link WhatsApp:\n\n"
                        f"<code>{qr_string[:200]}</code>",
                    )
            else:
                logger.info("No owner_chat_id configured — QR only visible in bridge terminal")

        elif msg_type == "error":
            logger.error(f"WhatsApp bridge error: {data.get('error')}")
