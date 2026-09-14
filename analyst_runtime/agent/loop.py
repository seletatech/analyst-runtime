"""Agent loop: the core processing engine."""

import asyncio
import hashlib
import json
import os
import re
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

import json_repair
from loguru import logger

from analyst_runtime.agent.analysis_context import AnalysisArtifactError, AnalysisArtifactStore
from analyst_runtime.agent.context import ContextBuilder
from analyst_runtime.agent.memory import ProtectedMemorySection
from analyst_runtime.agent.routing import RoutingError, RuntimeRequestRouter
from analyst_runtime.agent.steering import SteeringCoordinator
from analyst_runtime.agent.subagent import SubagentManager
from analyst_runtime.agent.telemetry import RunModelTelemetry
from analyst_runtime.agent.tool_profiles import register_workspace_analysis_tools
from analyst_runtime.agent.tools.analyze_image_tool import AnalyzeImageTool
from analyst_runtime.agent.tools.cron import CronTool
from analyst_runtime.agent.tools.filesystem import ReadFileTool
from analyst_runtime.agent.tools.message import MessageTool
from analyst_runtime.agent.tools.registry import ToolRegistry, register_integration_tools
from analyst_runtime.agent.tools.spawn import SpawnTool
from analyst_runtime.agent.tools.transcription_tool import TranscribeAudioTool
from analyst_runtime.agent.tools.tts_tool import TextToSpeechTool
from analyst_runtime.bus.events import InboundMessage, OutboundMessage
from analyst_runtime.bus.queue import MessageBus
from analyst_runtime.config.schema import ExecToolConfig
from analyst_runtime.cron.service import CronService
from analyst_runtime.providers.base import LLMProvider
from analyst_runtime.session.manager import HISTORY_SUMMARY_TYPE, Session, SessionManager
from analyst_runtime.utils.tool_calls import sanitize_tool_name
from analyst_runtime.workspace import WorkspaceConfiguration

_COMPOSIO_MISSING_CREDENTIALS_MARKER = "Local Composio credentials are required at "
_AWAITING_COMPOSIO_API_KEY = "awaiting_composio_api_key"
_PENDING_COMPOSIO_USER_REQUEST = "pending_composio_user_request"
_MONTHLY_ARTIFACT_ID_RE = re.compile(
    r"monthly-event-reconciliation:"
    r"\d{4}-(?:0[1-9]|1[0-2]):[0-9a-f]{12}"
)
_PQC_ANALYSIS_ID_RE = re.compile(r"pqc-defect-loss:[0-9a-f]{64}")
_CONFIRMED_SEMANTICS_MEMORY = ProtectedMemorySection(
    name="confirmed_manufacturing_semantics",
    start_marker="<!-- confirmed-manufacturing-semantics:start -->",
    end_marker="<!-- confirmed-manufacturing-semantics:end -->",
)
_EXPLICIT_SEMANTICS_CONFIRMATION_RE = re.compile(
    r"\s*确认(?:并)?按上述口径分析[。.!！]?\s*"
)
_SELF_CONTAINED_SEMANTICS_CONFIRMATION_RE = re.compile(
    r"\s*确认以下口径并分析\s*[：:]\s*\S.+\s*",
    re.DOTALL,
)

ProgressTool = dict[str, str]
ProgressCallback = Callable[[str | None, ProgressTool | str | None], Awaitable[None]]
ToolProfile = Literal["full", "trusted-analysis", "readonly"]


@dataclass(frozen=True)
class AgentLoopResult:
    content: str | None
    tools_used: list[str]
    terminal_reason: str
    iterations: int
    application_retry_count: int
    model_call_count: int
    provider_retry_count: int
    retry_count: int
    usage: dict[str, int]
    error_code: str | None = None

    def __iter__(self):
        yield self.content
        yield self.tools_used


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    ITERATION_LIMIT_ERROR_CODE = "ANALYST-RUNTIME-ITERATION-001"
    TOOL_PROTOCOL_ERROR_CODE = "ANALYST-RUNTIME-PROTOCOL-001"
    MODEL_PROFILE_ERROR_CODE = "ANALYST-RUNTIME-MODEL-001"
    MODEL_CREDENTIAL_ERROR_CODE = "ANALYST-RUNTIME-CREDENTIAL-001"
    PROVIDER_ERROR_CODE = "ANALYST-RUNTIME-PROVIDER-001"

    @classmethod
    def _support_error_message(cls, error_code: str) -> str:
        return f"分析未完成。Error Code: {error_code}。请将此错误码提供给技术支持。"

    @staticmethod
    def _content_sha256(value: Any) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _message_context_manifest(cls, messages: list[dict[str, Any]]) -> dict[str, Any]:
        items = []
        for message in messages:
            value = message.get("content")
            encoded = json.dumps(message, ensure_ascii=False, sort_keys=True, default=str)
            item = {
                "role": str(message.get("role") or ""),
                "sha256": cls._content_sha256(message),
                "content_sha256": cls._content_sha256(value),
                "chars": len(encoded),
            }
            if message.get("tool_calls"):
                item["tool_call_count"] = len(message["tool_calls"])
            items.append(item)
        return {
            "message_count": len(messages),
            "roles": [item["role"] for item in items],
            "messages": items,
        }

    @staticmethod
    def _explicit_analysis_reuse(content: str) -> bool:
        """Recognize an explicit request to keep using the prior result unchanged."""
        compact = re.sub(r"\s+", "", content)
        prior_markers = ("上一轮", "上一次", "上次", "刚才", "已有", "原有")
        reuse_markers = ("沿用", "继续使用", "保持", "基于")
        result_markers = ("结果", "分析", "口径", "范围")
        unchanged_markers = ("不变", "相同", "同一")
        references_prior = any(marker in compact for marker in prior_markers)
        requests_reuse = any(marker in compact for marker in reuse_markers)
        names_reused_state = any(marker in compact for marker in result_markers)
        says_unchanged = any(marker in compact for marker in unchanged_markers)
        return references_prior and requests_reuse and names_reused_state and says_unchanged

    @staticmethod
    def _explicit_followup_analysis(content: str) -> bool:
        """Recognize permission to read new evidence for a follow-up analysis."""
        compact = re.sub(r"\s+", "", content)
        evidence_actions = ("补查", "查阅", "调取", "读取", "检索")
        evidence_scopes = ("生产记录", "过程资料", "过程检验", "品质资料", "PQC")
        analysis_goals = ("原因分析", "根因分析", "过程分析")
        return any(action in compact for action in evidence_actions) and (
            any(scope in compact for scope in evidence_scopes)
            or any(goal in compact for goal in analysis_goals)
        )

    def _runtime_provenance(self) -> dict[str, str]:
        def file_hash(relative_path: str) -> str:
            path = self.workspace / relative_path
            return (
                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "unavailable"
            )

        def data_release_hash() -> str:
            data_root = self.workspace / "data"
            manifest = data_root / "manifest.json"
            if not manifest.is_file():
                return "unavailable"
            try:
                digest = hashlib.sha256(manifest.read_bytes())
                # ponytail: metadata fingerprint avoids re-reading 3 GB per turn; replace it
                # with a generated digest manifest if timestamp-preserving imports exist.
                for path in sorted(item for item in data_root.rglob("*") if item.is_file()):
                    stat = path.stat()
                    digest.update(
                        f"{path.relative_to(data_root)}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode()
                    )
                return digest.hexdigest()
            except OSError:
                return "unavailable"

        return {
            "product_commit": os.environ.get("ANALYST_RUNTIME_VERSION", "unavailable"),
            "image_digest": os.environ.get("ANALYST_RUNTIME_IMAGE_DIGEST", "unavailable"),
            "tool_profile": self.tool_profile,
            "agents_md_sha256": file_hash("AGENTS.md"),
            "workspace_data_manifest_sha256": data_release_hash(),
        }

    @classmethod
    def _trace_summary(cls, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return span-ready telemetry without prompts, reasoning, or business payloads."""
        summary: list[dict[str, Any]] = []
        for event in events:
            event_type = str(event.get("type") or "")
            base = {
                "type": event_type,
                "timestamp": event.get("timestamp"),
                "uuid": event.get("uuid"),
                "parent_uuid": event.get("parent_uuid"),
            }
            if event_type == "user_input":
                summary.append(
                    {**base, "content_sha256": cls._content_sha256(event.get("content"))}
                )
            elif event_type == "prompt_snapshot":
                tools = event.get("tools") if isinstance(event.get("tools"), list) else []
                summary.append(
                    {
                        **base,
                        "prompt_sha256": cls._content_sha256(event.get("content")),
                        "prompt_chars": len(str(event.get("content") or "")),
                        "tools_sha256": cls._content_sha256(tools),
                        "tool_count": len(tools),
                        "context": event.get("context") or {},
                    }
                )
            elif event_type == "model_call":
                tool_calls = event.get("tool_calls")
                names = []
                if isinstance(tool_calls, list):
                    names = [
                        str(call.get("function", {}).get("name") or "")
                        for call in tool_calls
                        if isinstance(call, dict) and isinstance(call.get("function"), dict)
                    ]
                summary.append(
                    {
                        **base,
                        "model": event.get("model"),
                        "iteration": event.get("iteration"),
                        "finish_reason": event.get("finish_reason"),
                        "usage": event.get("usage") or {},
                        "tool_names": names,
                        "context": event.get("context") or {},
                    }
                )
            elif event_type == "tool_result":
                summary.append(
                    {
                        **base,
                        "tool_call_id": event.get("tool_use_id"),
                        "tool_name": event.get("tool_name"),
                        "input_sha256": cls._content_sha256(event.get("tool_input")),
                        "output_sha256": cls._content_sha256(event.get("content")),
                        "status": event.get("status") or "completed",
                    }
                )
            elif event_type == "analysis_context":
                summary.append(
                    {
                        **base,
                        "mode": event.get("mode"),
                        "analysis_id": event.get("analysis_id"),
                    }
                )
            elif event_type in {"context_compaction", "retry"}:
                summary.append(
                    {
                        **base,
                        **{
                            key: value
                            for key, value in event.items()
                            if key
                            in {
                                "iteration",
                                "model",
                                "prompt_tokens",
                                "messages_before",
                                "messages_after",
                                "reason",
                            }
                        },
                    }
                )
            elif event_type == "final_response":
                summary.append(
                    {
                        **base,
                        "model": event.get("model"),
                        "iterations": event.get("iterations"),
                        "finish_reason": event.get("finish_reason"),
                        "usage": event.get("usage") or {},
                        "tools_used": event.get("tools_used") or [],
                    }
                )
        return summary

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 500,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        memory_window: int = 50,
        brave_api_key: str | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        compress_after_turns: int = 30,
        compress_keep_turns: int = 15,
        consolidation_interval: int = 3600,
        consolidation_model: str | None = None,
        max_concurrent_messages: int = 4,
        context_compact_threshold: int = 80_000,
        context_compact_keep_messages: int = 12,
        tool_profile: ToolProfile = "full",
    ):
        if tool_profile not in {"full", "trusted-analysis", "readonly"}:
            raise ValueError(f"Unsupported Analyst Runtime tool profile: {tool_profile!r}")
        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.memory_window = memory_window
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace
        self.tool_profile = tool_profile
        workspace_configuration = WorkspaceConfiguration.load(workspace)
        self.request_router = RuntimeRequestRouter(
            provider=provider,
            workspace_configuration=workspace_configuration,
        )
        self.channel_manager = None  # set post-construction by the caller
        self.context = ContextBuilder(
            workspace,
            protected_memory_sections=(
                (_CONFIRMED_SEMANTICS_MEMORY,)
                if tool_profile == "trusted-analysis"
                else ()
            ),
            tool_profile=tool_profile,
        )
        self.sessions = session_manager or SessionManager(workspace)
        self.steering = SteeringCoordinator(
            bus=bus,
            sessions=self.sessions,
            context=self.context,
        )
        self.analysis_artifacts = AnalysisArtifactStore(workspace)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            brave_api_key=brave_api_key,
            exec_config=self.exec_config,
            restrict_to_workspace=(restrict_to_workspace or tool_profile == "trusted-analysis"),
        )

        self.compress_after_turns = compress_after_turns
        self.compress_keep_turns = compress_keep_turns
        self.consolidation_interval = consolidation_interval
        self.consolidation_model = consolidation_model
        self.max_concurrent_messages = max(1, max_concurrent_messages)
        self.context_compact_threshold = max(1, context_compact_threshold)
        self.context_compact_keep_messages = max(1, context_compact_keep_messages)
        # Per-session-key compression thresholds; heartbeat needs tighter limits
        # because its session grows daily from scheduled pings with no user value.
        self._session_compress_config: dict[str, tuple[int, int]] = {
            "heartbeat": (10, 5),
        }

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._consolidation_lock = asyncio.Lock()
        self._last_consolidation_time: dict[str, float] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._active_message_tasks: dict[str, asyncio.Task] = {}
        self._message_queues: dict[str, asyncio.Queue[InboundMessage]] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._message_semaphore = asyncio.Semaphore(self.max_concurrent_messages)
        self._pending_cancellations: set[str] = set()
        self.bus.add_inbound_listener(self._handle_inbound_control)
        self._register_default_tools()

    def _remember_confirmed_semantics(
        self,
        user_message: str,
        history: list[dict[str, Any]],
    ) -> str | None:
        confirms_previous_card = bool(
            _EXPLICIT_SEMANTICS_CONFIRMATION_RE.fullmatch(user_message)
        )
        supplies_complete_card = bool(
            _SELF_CONTAINED_SEMANTICS_CONFIRMATION_RE.fullmatch(user_message)
        )
        if self.tool_profile != "trusted-analysis" or not (
            confirms_previous_card or supplies_complete_card
        ):
            return None
        proposal_index = next(
            (
                index
                for index in range(len(history) - 1, -1, -1)
                if history[index].get("role") == "assistant"
                and "待确认的定义与口径"
                in str(history[index].get("content") or "")
            ),
            None,
        )
        if proposal_index is None:
            return None
        proposal = str(history[proposal_index].get("content") or "").strip()
        confirmed_semantics = user_message.strip() if supplies_complete_card else proposal
        question = next(
            (
                str(message.get("content") or "").strip()
                for message in reversed(history[:proposal_index])
                if message.get("role") == "user"
            ),
            "",
        )
        if not question:
            return None

        digest = hashlib.sha256(confirmed_semantics.encode()).hexdigest()[:12]
        content = (
            f"<!-- confirmed-query:{self._semantic_query_hash(question)} -->\n"
            f"**适用问题**：{question}\n\n{confirmed_semantics}"
        )
        current = self.context.memory.read_protected_section(
            _CONFIRMED_SEMANTICS_MEMORY
        )
        entries = re.findall(
            r"(?ms)^### ([0-9a-f]{12})\n(.*?)(?=^### |\Z)", current
        )
        entries = [(digest, content)] + [
            (entry_id, content.strip())
            for entry_id, content in entries
            if entry_id != digest
        ][:4]
        rendered = "\n\n".join(
            f"### {entry_id}\n{content}" for entry_id, content in entries
        )
        self.context.memory.replace_protected_section(
            _CONFIRMED_SEMANTICS_MEMORY,
            f"{_CONFIRMED_SEMANTICS_MEMORY.start_marker}\n"
            "## 已确认的制造语义\n\n"
            "按最近确认优先；只在业务对象和全部口径一致时复用。\n\n"
            f"{rendered}\n"
            f"{_CONFIRMED_SEMANTICS_MEMORY.end_marker}",
        )
        return question

    def _semantic_clarification_instruction(
        self,
        user_message: str,
        history: list[dict[str, Any]],
    ) -> str | None:
        if self.tool_profile != "trusted-analysis" or (
            _EXPLICIT_SEMANTICS_CONFIRMATION_RE.fullmatch(user_message)
            or _SELF_CONTAINED_SEMANTICS_CONFIRMATION_RE.fullmatch(user_message)
        ):
            return None
        proposal_index = next(
            (
                index
                for index in range(len(history) - 1, -1, -1)
                if history[index].get("role") == "assistant"
                and "待确认的定义与口径"
                in str(history[index].get("content") or "")
            ),
            None,
        )
        if proposal_index is None:
            return (
                "如果当前请求需要语义澄清，这是首次澄清：只生成“待确认的定义与口径”"
                "并提出最多三个会显著改变结果的问题，每个编号只能包含一个问题；不要"
                "展开完整口径表、不要要求固定回复措辞。若是单记录精确查询，则跳过澄清"
                "并立即调用检索工具。"
            )
        clarification_round = 1 + sum(
            message.get("role") == "user" for message in history[proposal_index + 1 :]
        )
        if clarification_round >= 2:
            return (
                "这是本次复杂分析的第二轮也是最后一轮语义澄清。整合用户已经提供的"
                "答案，对仍缺失的非关键项采用建议默认值，必须开始调用分析工具并在本轮"
                "交付结果；不得再生成确认卡或要求用户确认。"
            )
        return (
            "这是第一轮语义澄清回复。先简短确认已知内容；如果仍有会显著改变结果的"
            "缺口，只问最多两个剩余问题且不得重复整张口径卡。信息足够时立即开始分析。"
        )

    @staticmethod
    def _semantic_query_hash(value: str) -> str:
        return AnalysisArtifactStore.question_hash(value)[:16]

    @staticmethod
    def _legacy_semantic_query_hash(value: str) -> str:
        normalized = re.sub(r"[\W_]+", "", value.casefold())
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    def _confirmed_semantics_reuse_instruction(self, user_message: str) -> str | None:
        if self._confirmed_semantics_sha256(user_message) is None:
            return None
        return (
            "当前问题与长期记忆中一条已确认制造语义的适用问题完全匹配。"
            "必须直接复用对应口径并开始分析，不得再次要求用户确认；"
            "最终回答仍需写明本次采用的已确认口径。"
        )

    def _confirmed_semantics_sha256(self, user_message: str) -> str | None:
        if self.tool_profile != "trusted-analysis":
            return None
        memory = self.context.memory.read_protected_section(_CONFIRMED_SEMANTICS_MEMORY)
        markers = (
            f"<!-- confirmed-query:{self._semantic_query_hash(user_message)} -->",
            f"<!-- confirmed-query:{self._legacy_semantic_query_hash(user_message)} -->",
        )
        for _, content in re.findall(
            r"(?ms)^### ([0-9a-f]{12})\n(.*?)(?=^### |\Z)", memory
        ):
            if any(marker in content for marker in markers):
                return hashlib.sha256(content.strip().encode()).hexdigest()
        return None

    async def _handle_inbound_control(self, msg: InboundMessage) -> None:
        if msg.metadata.get("control") != "cancel":
            return
        execution_key = msg.execution_key
        self._pending_cancellations.add(execution_key)
        task = self._active_message_tasks.get(execution_key)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _track_task(self, coro) -> asyncio.Task:
        """Create a background task and track it; log exceptions on completion."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)

        def _on_done(t: asyncio.Task) -> None:
            self._background_tasks.discard(t)
            if not t.cancelled() and t.exception():
                logger.error("Background task failed: {}", t.exception())

        task.add_done_callback(_on_done)
        return task

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        if self.tool_profile == "readonly":
            return
        if self.tool_profile == "trusted-analysis":
            self._register_analysis_tools(restrict_to_workspace=True, audit_reads=True)
            return

        self._register_analysis_tools(restrict_to_workspace=self.restrict_to_workspace)

        # Product profiles stop above. The generic full profile additionally exposes
        # optional media and external-integration adapters.
        self.tools.register(TranscribeAudioTool())
        self.tools.register(
            TextToSpeechTool(self.workspace, send_callback=self.bus.publish_outbound)
        )
        self.tools.register(AnalyzeImageTool())
        register_integration_tools(self.tools, workspace=self.workspace)
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

    def _register_analysis_tools(
        self,
        *,
        restrict_to_workspace: bool,
        audit_reads: bool = False,
    ) -> None:
        """Register the single Analyst Runtime capability set used by Linghui."""
        register_workspace_analysis_tools(
            self.tools,
            workspace=self.workspace,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
            audit_reads=audit_reads,
        )

        # Message tool
        self._register_message_tool()

    def _register_message_tool(self) -> None:
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self.tool_profile != "full":
            return
        if self._mcp_connected or not self._mcp_servers:
            return
        self._mcp_connected = True
        from analyst_runtime.agent.tools.mcp import connect_mcp_servers

        self._mcp_stack = AsyncExitStack()
        await self._mcp_stack.__aenter__()
        await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)

    def _set_tool_context(
        self,
        channel: str,
        chat_id: str,
        session_key: str | None = None,
        user_message: str | None = None,
        inbound_turn_id: str | None = None,
        analysis_conversation_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """Update context for all tools that need routing info."""
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.set_context(channel, chat_id)

        if tts_tool := self.tools.get("text_to_speech"):
            if isinstance(tts_tool, TextToSpeechTool):
                tts_tool.set_context(channel, chat_id)

        if spawn_tool := self.tools.get("spawn"):
            if isinstance(spawn_tool, SpawnTool):
                spawn_tool.set_context(channel, chat_id)

        if cron_tool := self.tools.get("cron"):
            if isinstance(cron_tool, CronTool):
                cron_tool.set_context(channel, chat_id)

        if read_file_tool := self.tools.get("read_file"):
            if isinstance(read_file_tool, ReadFileTool):
                read_file_tool.set_conversation_context(analysis_conversation_id)

        for tool_name in self.tools.tool_names:
            tool = self.tools.get(tool_name)
            if tool is None or not tool_name.startswith("composio_"):
                continue
            if hasattr(tool, "set_context"):
                if session_key is not None:
                    tool.set_context(channel, chat_id, session_key=session_key)
                else:
                    tool.set_context(channel, chat_id)

    def _merge_persisted_session_metadata(self, session: Session) -> None:
        """Merge metadata written by tools using their own session manager."""
        persisted = SessionManager(self.workspace).get_or_create(session.key)
        for key, value in persisted.metadata.items():
            if self.tool_profile != "full" and key in {
                _AWAITING_COMPOSIO_API_KEY,
                _PENDING_COMPOSIO_USER_REQUEST,
                "composio",
            }:
                continue
            if (
                key in {_AWAITING_COMPOSIO_API_KEY, _PENDING_COMPOSIO_USER_REQUEST}
                and self._composio_credentials_path().exists()
            ):
                continue
            if key == "composio" and isinstance(value, dict):
                # Composio metadata is authoritative on disk, including the empty dict
                # written when a refresh clears stale state before recreating a session.
                session.metadata[key] = dict(value)
                continue

            if key not in session.metadata or not session.metadata[key]:
                session.metadata[key] = value

    @staticmethod
    def _extract_action_chips(content: str) -> tuple[str, list[list[dict]] | None]:
        """Strip <!-- CHIPS: [...] --> block from content and return (clean_content, keyboard).

        The keyboard format mirrors Telegram's InlineKeyboardMarkup rows:
        [[{"text": str, "callback_data": str}], ...]
        Returns (content, None) when no chips block is present or parsing fails.
        """
        match = re.search(r"<!--\s*CHIPS:\s*(\[.*?\])\s*-->", content, re.DOTALL)
        if not match:
            return content, None
        clean = content[: match.start()].rstrip()
        try:
            chips_data = json.loads(match.group(1))
            if not isinstance(chips_data, list):
                return clean, None
            keyboard = [
                [{"text": c["label"][:40], "callback_data": f"action:{c['action'][:100]}"}]
                for c in chips_data[:3]
                if isinstance(c, dict) and c.get("label") and c.get("action")
            ]
            return clean, keyboard if keyboard else None
        except Exception:
            return clean, None

    def _build_capability_card(self) -> str:
        """Build a personalized capability overview for /whatcanyoudo."""
        tool_names = " ".join(self.tools.tool_names)
        lines: list[str] = ["Here's what I can do for you:\n"]

        if "gmail" in tool_names:
            lines.append("📧 Read and send emails")
        if "google_calendar" in tool_names:
            lines.append("📅 Check your calendar and schedule events")
        if "slack" in tool_names:
            lines.append("💬 Post messages to Slack")
        if "notion" in tool_names:
            lines.append("📝 Read and write Notion pages")
        if "firecrawl_search" in tool_names or "web_fetch" in tool_names:
            lines.append("🔍 Search the web and read articles")
        if "cron" in tool_names:
            lines.append("⏰ Set reminders and recurring tasks")
        if "text_to_speech" in tool_names:
            lines.append("🔊 Reply with voice messages")

        lines.append("🧠 Remember things about you across conversations")
        lines.append("📁 Manage files in your workspace")
        lines.append("💭 Think through problems, write, brainstorm")
        lines.append("\nJust tell me what you need.")
        return "\n".join(lines)

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    @staticmethod
    def _looks_like_intermediate_text(text: str) -> bool:
        """Distinguish a planning pause from a complete post-tool answer."""
        normalized = text.strip()
        final_markers = (
            "任务已完成",
            "分析已完成",
            "最终结论",
            "一句话结论",
            "老板报告",
            "数据不足",
        )
        if any(marker in normalized for marker in final_markers):
            return False
        if re.search(r"(?m)^#{1,3}\s+", normalized) and len(normalized) >= 200:
            return False
        future_markers = (
            "我将",
            "我会继续",
            "接下来",
            "现在开始",
            "下一步我会",
            "let me",
            "i'll",
            "i will",
        )
        lowered = normalized.lower()
        return len(normalized) < 80 or any(marker in lowered for marker in future_markers)

    @staticmethod
    def _looks_like_incomplete_delivery(text: str) -> bool:
        """Reject summaries that claim the actual report was delivered elsewhere."""
        normalized = text.strip()
        missing_report_references = (
            "上一条报告",
            "上一条消息中交付",
            "上一条消息完整给出",
            "上文已给出",
            "报告正文已在前面",
            "完整报告已在前面",
        )
        if any(marker in normalized for marker in missing_report_references):
            return True

        summary_markers = ("最终交付摘要", "最终报告摘要", "交付摘要")
        report_sections = (
            "一句话结论",
            "可确认事实",
            "缺失数据与反证条件",
            "老板可能还没注意到",
            "下一步",
        )
        present_sections = sum(section in normalized for section in report_sections)
        return any(marker in normalized for marker in summary_markers) and present_sections < 3

    @staticmethod
    def _tool_progress(
        tool_call: Any,
        status: str,
        result: Any = None,
    ) -> ProgressTool:
        """Build a compact, redacted public description for one tool call."""
        name = sanitize_tool_name(tool_call.name)
        arguments = tool_call.arguments if isinstance(tool_call.arguments, dict) else {}
        detail = next(
            (
                str(arguments[key])
                for key in ("path", "command", "query", "url", "endpoint", "working_dir")
                if arguments.get(key) not in (None, "")
            ),
            "",
        )
        detail = re.sub(r"\s+", " ", detail).strip()
        detail = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", detail)
        detail = re.sub(
            r"(?i)\bauthorization\s*[:=]\s*(?:Bearer\s+)?[^\s'\"]+",
            "Authorization=[REDACTED]",
            detail,
        )
        detail = re.sub(
            r"(?i)\b(api[_-]?key|token|secret|password|credential)"
            r"\s*[:=]\s*[^\s&;]+",
            r"\1=[REDACTED]",
            detail,
        )
        detail = re.sub(
            r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@",
            r"\1[REDACTED]@",
            detail,
        )
        detail = re.sub(r"(?i)Bearer\s+[^\s'\"]+", "Bearer [REDACTED]", detail)
        detail = detail[:160] + ("…" if len(detail) > 160 else "")

        if name in {"read_file", "list_dir", "web_fetch"}:
            kind = "read"
        elif name in {"write_file", "append_file", "patch_file", "edit_file"}:
            kind = "write"
        elif "search" in name:
            kind = "search"
        elif name in {"exec", "spawn"}:
            kind = "execute"
        elif "analy" in name:
            kind = "analyze"
        else:
            kind = "other"

        progress = {
            "detail": detail,
            "id": str(tool_call.id),
            "kind": kind,
            "name": name,
            "status": status,
        }
        if status == "completed":
            evidence = AgentLoop._tool_result_evidence(result)
            if evidence:
                progress["evidence"] = evidence
        return progress

    @staticmethod
    def _tool_call_succeeded(result: Any) -> bool:
        """Conservatively classify terminal tool results for public progress evidence."""
        if not isinstance(result, str) or not result.strip():
            return False
        normalized = result.strip()
        if re.search(r"(?i)^\s*(?:error|failed|failure)\b", normalized):
            return False
        if "Traceback (most recent call last):" in normalized:
            return False
        if re.search(r"(?im)^\s*exit code:\s*[1-9]\d*\s*$", normalized):
            return False
        try:
            payload = json.loads(normalized)
        except (TypeError, json.JSONDecodeError):
            payload = None
        if not isinstance(payload, dict):
            return not any(
                re.search(pattern, normalized)
                for pattern in (
                    r"(?i)^.{0,120}\b(?:failed|failure|timed out|timeout|"
                    r"unauthorized|forbidden|permission denied|connection reset)\b",
                    r"(?i)(?:^|\n)\s*(?:exit|return|status)\s*code\s*[:=]?\s*"
                    r"[1-9]\d*\b",
                    r"(?i)\bnon[- ]?zero(?:\s+(?:exit|return)?\s*"
                    r"(?:code|status))?\s*[:=]?\s*[1-9]\d*\b",
                    r"(?i)\bHTTP(?:/[0-9.]+)?\s+[45]\d{2}\b",
                    r"(?i)^\s*transport\s+error\b",
                )
            )
        status = str(payload.get("status") or "").strip().lower()
        if status in {"error", "failed", "failure", "auth_required", "skipped"}:
            return False
        if payload.get("error") not in (None, False, ""):
            return False
        if payload.get("success") is False or payload.get("ok") is False:
            return False
        status_code = payload.get("status_code")
        if isinstance(status_code, int) and status_code >= 400:
            return False
        if set(payload) == {"detail"}:
            return False
        return True

    @staticmethod
    def _tool_result_evidence(result: Any) -> str:
        """Extract a single non-secret artifact identity from a successful JSON result."""
        if not AgentLoop._tool_call_succeeded(result):
            return ""
        try:
            payload = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            if not isinstance(result, str) or "... (truncated," not in result:
                return ""
            analysis_match = re.search(
                r'"analysis_id"\s*:\s*"(?P<analysis_id>pqc-defect-loss:[0-9a-f]{64})"',
                result,
            )
            pqc_schema = bool(
                re.search(r'"schema_version"\s*:\s*"linghui-pqc-defect-loss/v1"', result)
            )
            pqc_complete = bool(re.search(r'"status"\s*:\s*"complete"', result))
            if analysis_match and pqc_schema and pqc_complete:
                return f"analysis_id={analysis_match.group('analysis_id')}"
            artifact_match = re.search(
                r'"artifact_id"\s*:\s*"(?P<artifact_id>'
                r"monthly-event-reconciliation:"
                r'\d{4}-(?:0[1-9]|1[0-2]):[0-9a-f]{12})"',
                result,
            )
            passed_gate = re.search(
                r'"release_gate"\s*:\s*\{[^{}]*"passed"\s*:\s*true',
                result,
            )
            if not artifact_match or not passed_gate:
                return ""
            return f"artifact_id={artifact_match.group('artifact_id')}"
        if not isinstance(payload, dict):
            return ""
        analysis_id = payload.get("analysis_id")
        if (
            payload.get("schema_version") == "linghui-pqc-defect-loss/v1"
            and payload.get("status") == "complete"
            and isinstance(analysis_id, str)
            and _PQC_ANALYSIS_ID_RE.fullmatch(analysis_id)
        ):
            return f"analysis_id={analysis_id}"
        artifact_id = payload.get("artifact_id")
        if not isinstance(artifact_id, str) or not _MONTHLY_ARTIFACT_ID_RE.fullmatch(artifact_id):
            return ""
        release_gate = payload.get("release_gate")
        if not isinstance(release_gate, dict) or release_gate.get("passed") is not True:
            return ""
        return f"artifact_id={artifact_id}"

    def _bind_analysis_context(
        self,
        session: Session,
        result: Any,
        *,
        parent_uuid: str | None,
    ) -> str | None:
        evidence = self._tool_result_evidence(result)
        if not evidence.startswith("analysis_id="):
            return parent_uuid
        analysis_id = evidence.removeprefix("analysis_id=")
        if session.metadata.get("active_analysis_id") == analysis_id:
            return parent_uuid
        try:
            self.analysis_artifacts.load(analysis_id)
        except AnalysisArtifactError as error:
            logger.warning("Refused to bind analysis context {}: {}", analysis_id, error)
            return parent_uuid

        event_uuid = str(uuid.uuid4())
        session.metadata["active_analysis_id"] = analysis_id
        session.add_event(
            {
                "uuid": event_uuid,
                "parent_uuid": parent_uuid,
                "type": "analysis_context",
                "mode": "created",
                "analysis_id": analysis_id,
            }
        )
        self.sessions.save(session)
        return event_uuid

    def _reuse_analysis_context(
        self,
        session: Session,
        *,
        parent_uuid: str,
        question: str | None = None,
        data_manifest_sha256: str | None = None,
        confirmed_semantics_sha256: str | None = None,
    ) -> tuple[str | None, bool]:
        analysis_id = session.metadata.get("active_analysis_id")
        if not isinstance(analysis_id, str):
            return None, False
        try:
            payload = self.analysis_artifacts.load(analysis_id)
        except AnalysisArtifactError as error:
            logger.warning("Bound analysis context {} is unavailable: {}", analysis_id, error)
            return (
                "A prior analysis is referenced by this conversation, but its approved "
                "artifact is unavailable. Do not reuse numeric claims from chat history. "
                "Explain that the analysis must be run again before quoting a result.",
                False,
            )
        if payload.get("schema_version") == "analyst-runtime-answer/v1" and (
            question is None
            or data_manifest_sha256 is None
            or confirmed_semantics_sha256 is None
            or not self.analysis_artifacts.matches_completed_answer(
                analysis_id,
                question=question,
                data_manifest_sha256=data_manifest_sha256,
                confirmed_semantics_sha256=confirmed_semantics_sha256,
            )
        ):
            return None, False

        session.add_event(
            {
                "uuid": str(uuid.uuid4()),
                "parent_uuid": parent_uuid,
                "type": "analysis_context",
                "mode": "reused",
                "analysis_id": analysis_id,
            }
        )
        return self.analysis_artifacts.system_instruction(payload), True

    @staticmethod
    def _guarded_tool_call_signature(name: str, arguments: Any) -> str | None:
        """Return a stable signature for external calls that must not repeat in one turn."""
        if name != "composio_execute_tools" or not isinstance(arguments, dict):
            return None
        canonical_arguments = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"{name}:{canonical_arguments}"

    @staticmethod
    def _guarded_tool_call_succeeded(result: Any) -> bool:
        """Only cache completed external calls; error results remain retryable."""
        if not isinstance(result, str):
            return False
        try:
            payload = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            return False
        return (
            isinstance(payload, dict)
            and not payload.get("error")
            and payload.get("status") != "error"
        )

    @staticmethod
    def _large_file_retry_hint(
        tool_name: str, arguments: dict[str, Any], result: str
    ) -> str | None:
        """Return an extra recovery hint when large file tool arguments lose content."""
        if tool_name not in {"write_file", "append_file"}:
            return None
        if "missing required content" not in result:
            return None

        path = arguments.get("path")
        if not isinstance(path, str) or not path:
            return None

        return (
            f"Your last {tool_name} call for {path} was missing the required content argument. "
            "That usually means the file body was too large and got dropped during tool-call generation. "
            "Do not use exec, shell redirection, cat, tee, Python, or Node to write the file. "
            "Retry with append_file using the same path and keep each content chunk under 8000 characters. "
            "If you need to reset the file first, call write_file with the same path and an empty string, "
            "then continue with append_file chunks. Use patch_file only for small targeted repairs."
        )

    # Inline result threshold: results shorter than this are stored verbatim in
    # the session event so the LLM sees full context on subsequent turns.
    # Longer results are truncated to this length inline; the full content is
    # archived to disk for read_file access.
    # 16,000 matches the upstream analyst_runtime default for max_tool_result_chars.
    _INLINE_RESULT_CHARS = 16_000

    def _save_tool_result(self, tool_call_id: str, tool_name: str, result: str) -> str:
        """Return inline content for session storage; archive long results to disk.

        Short results (≤ _INLINE_RESULT_CHARS) are returned as-is so history
        reconstruction always provides meaningful context to the LLM.

        Long results: the first _INLINE_RESULT_CHARS chars are returned inline so
        the LLM has context in subsequent turns, and the full result is archived
        to sessions/tool-results/{id}.txt for read_file access when needed.
        """
        if len(result) <= self._INLINE_RESULT_CHARS:
            return result  # short result: inline, no file needed

        # Long result: archive full content and return truncated preview
        tool_results_dir = self.workspace / "sessions" / "tool-results"
        tool_results_dir.mkdir(parents=True, exist_ok=True)
        # Sanitize tool_call_id to prevent path traversal — keep only safe chars
        safe_id = "".join(c for c in tool_call_id if c.isalnum() or c in "-_")
        filename = f"{safe_id}.txt"
        try:
            (tool_results_dir / filename).write_text(result, encoding="utf-8")
            return (
                f"{result[: self._INLINE_RESULT_CHARS]}\n"
                f"[...{len(result):,} chars total — full result at "
                f"sessions/tool-results/{filename}]"
            )
        except Exception as exc:
            logger.warning("Failed to save tool result {}: {}", tool_call_id, exc)
            return result  # fall back to full inline if archive fails

    def _session_tool_result_content(
        self,
        tool_call_id: str,
        tool_name: str,
        result: str,
    ) -> str:
        """Persist an approved analysis pointer instead of its full business payload."""
        evidence = self._tool_result_evidence(result)
        if evidence.startswith("analysis_id="):
            analysis_id = evidence.removeprefix("analysis_id=")
            try:
                self.analysis_artifacts.load(analysis_id)
            except AnalysisArtifactError:
                pass
            else:
                return evidence
        return self._save_tool_result(tool_call_id, tool_name, result)

    def _composio_credentials_path(self) -> Path:
        return self.workspace / ".analyst-runtime" / "composio" / "credentials.json"

    @staticmethod
    def _looks_like_composio_api_key(content: str) -> bool:
        candidate = content.strip()
        if not candidate or len(candidate) < 20:
            return False
        if "\n" in candidate or any(char.isspace() for char in candidate):
            return False
        return any(char.isalpha() for char in candidate) and any(
            char.isdigit() for char in candidate
        )

    @staticmethod
    def _extract_request_text(session: Session, request_uuid: str | None) -> str:
        if not request_uuid:
            return ""
        for event in reversed(session.events):
            if event.get("uuid") != request_uuid:
                continue
            if event.get("type") != "user_input":
                continue
            return str(event.get("content") or "").strip()
        return ""

    @staticmethod
    def _is_composio_missing_credentials_result(tool_name: str, result: str) -> bool:
        if not tool_name.startswith("composio_") or not isinstance(result, str):
            return False
        if _COMPOSIO_MISSING_CREDENTIALS_MARKER in result:
            return True
        try:
            payload = json.loads(result)
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        return _COMPOSIO_MISSING_CREDENTIALS_MARKER in str(payload.get("error") or "")

    @staticmethod
    def _mark_awaiting_composio_api_key(session: Session, pending_request: str) -> None:
        session.metadata[_AWAITING_COMPOSIO_API_KEY] = True
        if pending_request:
            session.metadata[_PENDING_COMPOSIO_USER_REQUEST] = pending_request

    @staticmethod
    def _clear_composio_api_key_pending(session: Session) -> None:
        session.metadata.pop(_AWAITING_COMPOSIO_API_KEY, None)
        session.metadata.pop(_PENDING_COMPOSIO_USER_REQUEST, None)

    def _composio_bootstrap_instruction(self, session: Session, current_message: str) -> str | None:
        if self.tool_profile != "full":
            return None
        if not session.metadata.get(_AWAITING_COMPOSIO_API_KEY):
            return None
        if self._composio_credentials_path().exists():
            return None
        if not self._looks_like_composio_api_key(current_message):
            return None

        pending_request = str(session.metadata.get(_PENDING_COMPOSIO_USER_REQUEST) or "").strip()
        if not pending_request:
            return None

        credentials_path = self._composio_credentials_path()
        return (
            "## Composio Bootstrap\n"
            "The user's current message is their Composio API key.\n"
            f"Use `write_file` to write JSON to `{credentials_path}` with this exact shape: "
            '{"api_key":"<current user message>"}.\n'
            f"Then continue this pending request in the same turn: {pending_request}\n"
            "Do not ask for the key again. Do not echo it back. Do not treat the current user message as a new task."
        )

    @staticmethod
    def _append_system_instruction(messages: list[dict[str, Any]], instruction: str | None) -> None:
        if not instruction or not messages:
            return
        content = messages[0].get("content")
        if not isinstance(content, list):
            return
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                item["text"] = f"{item.get('text', '')}\n\n{instruction}".strip()
                return

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: ProgressCallback | None = None,
        session: Session | None = None,
        request_uuid: str | None = None,
        model: str | None = None,
        execution_key: str | None = None,
        allow_tools: bool = True,
    ) -> AgentLoopResult:
        """
        Run the agent iteration loop.

        Args:
            initial_messages: Starting messages for the LLM conversation.
            on_progress: Optional callback to push intermediate content to the user.
            session: Session to emit events into (optional).
            request_uuid: UUID of the user_input event; used as initial parent_uuid
                          for the event causal chain.

        Returns:
            Tuple of (final_content, list_of_tools_used).
        """
        active_model = model or self.model
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []
        parent_uuid = request_uuid
        _consecutive_malformed = 0
        _total_malformed = 0  # all-malformed iterations this turn (hard safety cap)
        _real_iterations = 0  # iterations with at least one real (non-malformed) tool call
        _had_midtask_continuation = False  # one-shot: prevents infinite retry on text-only stops
        _delivery_recovery_attempts = 0  # bounded retries for missing final report bodies
        _completed_guarded_tool_calls: dict[str, str] = {}
        terminal_reason = "iteration_limit"
        terminal_error_code: str | None = None
        model_telemetry = RunModelTelemetry()

        def record_retry(reason: str) -> None:
            model_telemetry.record_application_retry()
            if session and request_uuid:
                session.add_event(
                    {
                        "uuid": str(uuid.uuid4()),
                        "parent_uuid": request_uuid,
                        "type": "retry",
                        "model": active_model,
                        "iteration": iteration,
                        "reason": reason,
                    }
                )

        while iteration < self.max_iterations:
            iteration += 1
            available_tools = self.tools.get_definitions() if allow_tools else []

            response = await self.provider.chat(
                messages=messages,
                tools=available_tools,
                model=active_model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
            if session and request_uuid:
                session.add_event(
                    {
                        "uuid": str(uuid.uuid4()),
                        "parent_uuid": request_uuid,
                        "type": "model_call",
                        "model": active_model,
                        "iteration": iteration,
                        "finish_reason": response.finish_reason,
                        "usage": response.usage,
                        "provider_retry_count": response.retry_count,
                        "context": {
                            **self._message_context_manifest(messages),
                            "available_tools_sha256": self._content_sha256(available_tools),
                            "available_tool_count": len(available_tools),
                        },
                        "tool_calls": [
                            {
                                "function": {"name": sanitize_tool_name(tool_call.name)},
                                "id": tool_call.id,
                            }
                            for tool_call in response.tool_calls
                        ],
                    }
                )
                for _ in range(max(0, response.retry_count)):
                    session.add_event(
                        {
                            "uuid": str(uuid.uuid4()),
                            "parent_uuid": request_uuid,
                            "type": "retry",
                            "model": active_model,
                            "iteration": iteration,
                            "reason": "provider_retry",
                        }
                    )
            model_telemetry.record(response)

            if response.finish_reason == "error":
                terminal_reason = "provider_error"
                terminal_error_code = response.error_code or self.PROVIDER_ERROR_CODE
                final_content = None
                break

            if response.has_tool_calls:
                if on_progress:
                    # Expose provider-authored public summaries without leaking private
                    # reasoning_content.
                    clean = self._strip_think(response.content)
                    if clean:
                        await on_progress(clean, None)

                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": sanitize_tool_name(tc.name),
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages,
                    response.content,
                    tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                )

                # Detect hallucinated tool-use IDs used as tool names BEFORE saving to
                # session so we can skip persisting all-malformed iterations entirely.
                # Covers both Bedrock format (tooluse_XYZ) and Claude API format (toolu_XYZ).
                _tooluse_id_re = re.compile(r"^(tooluse|toolu)_[A-Za-z0-9]{10,}$")
                _malformed_names = [
                    tc.name for tc in response.tool_calls if _tooluse_id_re.match(tc.name or "")
                ]
                _all_malformed = bool(_malformed_names) and len(_malformed_names) == len(
                    response.tool_calls
                )

                # Emit llm_response event — skipped for all-malformed iterations to avoid
                # poisoning next-turn history with confusing tool_use → "not found" sequences.
                llm_uuid = str(uuid.uuid4())
                if session and request_uuid and not _all_malformed:
                    session.add_event(
                        {
                            "uuid": llm_uuid,
                            "parent_uuid": parent_uuid,
                            "type": "llm_response",
                            "message_id": response.message_id,
                            "model": active_model,
                            "iteration": iteration,
                            "content": response.content,
                            "reasoning": response.reasoning_content,
                            "tool_calls": tool_call_dicts,
                            "usage": response.usage,
                            "finish_reason": response.finish_reason,
                        }
                    )
                    parent_uuid = llm_uuid

                pre_results = await self.tools.pre_execute(response.tool_calls)

                if _malformed_names:
                    logger.warning(
                        "Model returned tool call IDs as tool names — likely wrong model "
                        "routing or provider mismatch. Names: {} (model={})",
                        _malformed_names,
                        active_model,
                    )

                # Only save the llm_response to session if it contains at least one real
                # tool call.  All-malformed iterations are noise — persisting them poisons
                # the history reconstructed on the next turn, causing subsequent turns to
                # start in a confused state and immediately generate more malformed calls.
                _persist_session = session and request_uuid and not _all_malformed

                for tool_call in response.tool_calls:
                    sanitized_name = sanitize_tool_name(tool_call.name)
                    tools_used.append(sanitized_name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    guarded_signature = self._guarded_tool_call_signature(
                        sanitized_name,
                        tool_call.arguments,
                    )
                    logger.info(f"Tool call: {sanitized_name}({args_str[:200]})")
                    if on_progress:
                        await on_progress(None, self._tool_progress(tool_call, "running"))
                    if guarded_signature and guarded_signature in _completed_guarded_tool_calls:
                        result = _completed_guarded_tool_calls[guarded_signature]
                        logger.warning(
                            "Suppressed duplicate successful external tool call in the same turn: {}",
                            sanitized_name,
                        )
                    elif pre_result := pre_results.get(tool_call.id):
                        result = pre_result  # auth_required or skipped — do not execute
                    else:
                        result = await self.tools.execute(sanitized_name, tool_call.arguments)
                        if guarded_signature and self._guarded_tool_call_succeeded(result):
                            _completed_guarded_tool_calls[guarded_signature] = result
                    logger.debug(
                        "Tool result: {}({}) → {}",
                        sanitized_name,
                        args_str[:80],
                        result[:200] if isinstance(result, str) else str(result)[:200],
                    )
                    terminal_status = (
                        "completed" if self._tool_call_succeeded(result) else "failed"
                    )
                    if on_progress:
                        await on_progress(
                            None,
                            self._tool_progress(tool_call, terminal_status, result),
                        )
                    # Bound results on first insertion, not on every later call:
                    # this keeps the growing prompt prefix stable for provider caching.
                    # Full evidence remains available in the archive and to binding below.
                    context_result = self._save_tool_result(
                        tool_call.id, sanitized_name, result
                    )
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, sanitized_name, context_result
                    )
                    if retry_hint := self._large_file_retry_hint(
                        sanitized_name, tool_call.arguments, result
                    ):
                        messages.append({"role": "system", "content": retry_hint})
                    if session and self._is_composio_missing_credentials_result(
                        sanitized_name, result
                    ):
                        pending_request = self._extract_request_text(session, request_uuid)
                        self._mark_awaiting_composio_api_key(session, pending_request)

                    # Persist tool_result to session so get_history() can reconstruct
                    # a valid tool_use → tool_result sequence on the next turn.
                    # Without this, Bedrock rejects subsequent requests:
                    # "tool_use ids were found without tool_result blocks immediately after"
                    if _persist_session:
                        tr_uuid = str(uuid.uuid4())
                        ref_content = self._session_tool_result_content(
                            tool_call.id,
                            tool_call.name,
                            result,
                        )
                        session.add_event(
                            {
                                "uuid": tr_uuid,
                                "parent_uuid": parent_uuid,
                                "type": "tool_result",
                                "tool_use_id": tool_call.id,
                                "source_assistant_uuid": llm_uuid,
                                "tool_name": sanitized_name,
                                "tool_input": tool_call.arguments,
                                "content": ref_content,
                                "status": terminal_status,
                            }
                        )
                        parent_uuid = tr_uuid
                    if session:
                        parent_uuid = self._bind_analysis_context(
                            session,
                            result,
                            parent_uuid=parent_uuid,
                        )

                steered_messages = await self.steering.apply_pending(
                    execution_key,
                    messages,
                    session=session,
                    parent_uuid=parent_uuid,
                )
                if steered_messages:
                    messages = steered_messages
                    final_content = None
                    continue

                # If all tool calls are malformed ID-style names, count failures and
                # inject a correction prompt so the model can retry.
                #
                # Abort threshold is dynamic:
                # - No real work yet (model stuck from the start): abort after 2 consecutive.
                # - Real work in progress (Kimi "thinking pause" between batches): allow up
                #   to 5 consecutive before aborting — Kimi k2.5 often emits 2-3 malformed
                #   "transition" calls between real exec batches and recovers on its own.
                # Hard cap: abort unconditionally after 8 total all-malformed iterations
                # to prevent infinite junk loops regardless of real-work history.
                if _all_malformed:
                    _consecutive_malformed += 1
                    _total_malformed += 1
                    consecutive_limit = 2 if _real_iterations == 0 else 5
                    if _total_malformed >= 8 or _consecutive_malformed >= consecutive_limit:
                        terminal_reason = "tool_protocol"
                        logger.warning(
                            "Aborting loop after {} consecutive / {} total malformed tool call "
                            "iterations (real_iterations={}, model={})",
                            _consecutive_malformed,
                            _total_malformed,
                            _real_iterations,
                            active_model,
                        )
                        break
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                "Your tool call names were invalid — you used tool use IDs "
                                "instead of actual tool names. Please retry the action using "
                                "the correct tool name from the available tools list."
                            ),
                        }
                    )
                    if iteration < self.max_iterations:
                        record_retry("malformed_tool_call")
                else:
                    _consecutive_malformed = 0
                    _real_iterations += 1
                messages_before_compaction = messages
                messages = await self._maybe_compact_active_context(
                    messages,
                    response.usage,
                    model=active_model,
                    telemetry=model_telemetry,
                )
                if messages is not messages_before_compaction and session and request_uuid:
                    session.add_event(
                        {
                            "uuid": str(uuid.uuid4()),
                            "parent_uuid": request_uuid,
                            "type": "context_compaction",
                            "model": active_model,
                            "iteration": iteration,
                            "prompt_tokens": response.usage.get("prompt_tokens", 0),
                            "messages_before": len(messages_before_compaction),
                            "messages_after": len(messages),
                        }
                    )
            elif response.finish_reason == "length":
                # Model hit max_tokens before finishing — inject a continue prompt
                # and keep looping rather than breaking mid-task.
                logger.warning(
                    "LLM hit max_tokens (finish_reason=length) at iteration {} "
                    "(model={}, tools_used_so_far={}). Injecting continue prompt.",
                    iteration,
                    active_model,
                    tools_used,
                )
                truncated_content = self._strip_think(response.content)
                messages = self.context.add_assistant_message(
                    messages,
                    truncated_content or "The previous response reached its output limit.",
                    None,
                    reasoning_content=response.reasoning_content,
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was cut off because it hit the token limit. "
                            "Resume exactly where you left off — do not restart, re-announce, "
                            "or repeat work already completed."
                        ),
                    }
                )
                messages_before_compaction = messages
                messages = await self._maybe_compact_active_context(
                    messages,
                    response.usage,
                    model=active_model,
                    telemetry=model_telemetry,
                )
                if messages is not messages_before_compaction and session and request_uuid:
                    session.add_event(
                        {
                            "uuid": str(uuid.uuid4()),
                            "parent_uuid": request_uuid,
                            "type": "context_compaction",
                            "model": active_model,
                            "iteration": iteration,
                            "prompt_tokens": response.usage.get("prompt_tokens", 0),
                            "messages_before": len(messages_before_compaction),
                            "messages_after": len(messages),
                        }
                    )
                if iteration < self.max_iterations:
                    record_retry("output_length")
            else:
                final_content = self._strip_think(response.content)

                # Some models (e.g. Kimi k2.5) finish a turn with only <think> tokens
                # and no visible text after completing tool calls.  Rather than falling
                # through to the "completed processing" fallback, inject one recovery
                # prompt so the model actually replies to the user.
                if final_content is None and tools_used and iteration < self.max_iterations:
                    logger.warning(
                        "Model returned no text after tool use — injecting summary prompt "
                        "(iteration={}, model={})",
                        iteration,
                        active_model,
                    )
                    messages = self.context.add_assistant_message(
                        messages,
                        response.content or "(thinking...)",
                        None,
                        reasoning_content=response.reasoning_content,
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": "Please respond to the user based on your findings.",
                        }
                    )
                    record_retry("missing_final_text")
                    continue

                if (
                    final_content is not None
                    and tools_used
                    and _delivery_recovery_attempts < 2
                    and self._looks_like_incomplete_delivery(final_content)
                    and iteration < self.max_iterations
                ):
                    _delivery_recovery_attempts += 1
                    logger.warning(
                        "Model returned an incomplete final delivery at iteration {} "
                        "— requesting the self-contained report (attempt {}/2, model={})",
                        iteration,
                        _delivery_recovery_attempts,
                        active_model,
                    )
                    messages = self.context.add_assistant_message(
                        messages,
                        final_content,
                        None,
                        reasoning_content=response.reasoning_content,
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "The report body is not visible to the user. Return the complete, "
                                "self-contained final report now: include all required Markdown "
                                "sections, detailed tables, conclusions, limitations, next steps, "
                                "and every artifact image URL. Do not refer to any previous message "
                                "or provide another summary. Do not call more tools unless a factual "
                                "correction is required."
                            ),
                        }
                    )
                    final_content = None
                    record_retry("incomplete_delivery")
                    continue

                # Kimi k2.5 sometimes emits a text-only "I'll do X next" announcement
                # without including the tool call (finish_reason=stop, has_content=True,
                # no tool_calls).  The loop would exit here and deliver an incomplete
                # response.  Give the model one second chance: if it again returns
                # text-only, that IS the final answer and we break normally.
                if (
                    final_content is not None
                    and tools_used
                    and not _had_midtask_continuation
                    and self._looks_like_intermediate_text(final_content)
                    and iteration < self.max_iterations
                ):
                    logger.warning(
                        "Model returned text-only mid-task (finish_reason=stop, no tool calls) "
                        "at iteration {} — injecting continuation prompt (model={})",
                        iteration,
                        active_model,
                    )
                    _had_midtask_continuation = True  # one shot only
                    messages = self.context.add_assistant_message(
                        messages,
                        final_content,
                        None,
                        reasoning_content=response.reasoning_content,
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "If you still need to take actions to complete this task, "
                                "include the tool calls now. If the task is complete, "
                                "give your final answer."
                            ),
                        }
                    )
                    final_content = None  # don't emit final_response yet
                    record_retry("text_only_midtask")
                    continue

                steered_messages = await self.steering.apply_pending(
                    execution_key,
                    messages,
                    session=session,
                    parent_uuid=parent_uuid,
                    preceding_assistant=response.content,
                )
                if steered_messages:
                    messages = steered_messages
                    final_content = None
                    continue

                logger.info(
                    "Agent loop ended at iteration {}: finish_reason={} tools_used={} "
                    "has_content={} (model={})",
                    iteration,
                    response.finish_reason,
                    tools_used,
                    bool(final_content),
                    active_model,
                )
                terminal_reason = "completed"
                # Emit final_response event
                if session and request_uuid:
                    session.add_event(
                        {
                            "uuid": str(uuid.uuid4()),
                            "parent_uuid": parent_uuid,
                            "type": "final_response",
                            "message_id": response.message_id,
                            "model": active_model,
                            "content": final_content,
                            "reasoning": response.reasoning_content,
                            "usage": response.usage,
                            "tools_used": tools_used if tools_used else None,
                            "iterations": iteration,
                            "finish_reason": response.finish_reason,
                        }
                    )
                break

        return AgentLoopResult(
            content=final_content,
            tools_used=tools_used,
            terminal_reason=terminal_reason,
            iterations=iteration,
            application_retry_count=model_telemetry.application_retry_count,
            model_call_count=model_telemetry.model_call_count,
            provider_retry_count=model_telemetry.provider_retry_count,
            retry_count=model_telemetry.retry_count,
            usage=model_telemetry.usage,
            error_code=terminal_error_code,
        )

    async def _maybe_compact_active_context(
        self,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
        *,
        model: str,
        telemetry: RunModelTelemetry | None = None,
    ) -> list[dict[str, Any]]:
        """Replace old model context with a summary after the token threshold."""
        prompt_tokens = int(
            usage.get("prompt_tokens")
            or usage.get("input_tokens")
            or usage.get("total_input_tokens")
            or 0
        )
        if prompt_tokens < self.context_compact_threshold:
            return messages

        compacted = await self._compact_active_context(
            messages,
            model=model,
            telemetry=telemetry,
        )
        if compacted is messages:
            return messages
        logger.info(
            "Active context compacted at {} prompt tokens: {} messages -> {} messages",
            prompt_tokens,
            len(messages),
            len(compacted),
        )
        return compacted

    async def _compact_active_context(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        telemetry: RunModelTelemetry | None = None,
    ) -> list[dict[str, Any]]:
        """Summarize older messages while preserving system rules and recent work."""
        leading_system_count = 0
        for message in messages:
            if message.get("role") != "system":
                break
            leading_system_count += 1

        keep = min(self.context_compact_keep_messages, len(messages))
        cut_at = max(leading_system_count, len(messages) - keep)
        if cut_at == leading_system_count:
            # A single tool result can exceed the threshold before the message
            # count reaches the recent-tail allowance. Compact the whole active
            # exchange rather than leaving that oversized result untouched.
            cut_at = len(messages)
        # Never split an assistant tool-call block from its tool results. A tail
        # beginning with a tool message is invalid for OpenAI-compatible APIs.
        while (
            cut_at < len(messages)
            and cut_at > leading_system_count
            and messages[cut_at].get("role") == "tool"
        ):
            cut_at -= 1
        old_messages = messages[leading_system_count:cut_at]
        if not old_messages:
            return messages

        transcript = json.dumps(old_messages, ensure_ascii=False, default=str)
        compact_prompt = (
            "Summarize the earlier portion of an AI agent run so it can continue "
            "without the raw messages. Preserve all information needed to finish the task: "
            "the user's goal and constraints; verified facts and exact identifiers; decisions "
            "and assumptions; files, artifacts, commands, and tool results; work completed; "
            "failures and attempted approaches; unresolved questions; and the precise next "
            "actions. Never invent facts. Prefer concise structured prose, but retain exact "
            "numbers, paths, names, and status where they matter. Return only the summary.\n\n"
            f"Earlier messages:\n{transcript}"
        )
        try:
            response = await self.provider.chat(
                messages=[
                    {
                        "role": "system",
                        "content": "You are a context compaction engine. Return only a faithful replacement summary.",
                    },
                    {"role": "user", "content": compact_prompt},
                ],
                # Active-context compaction is part of the current billed run.
                # Use the run model so its usage and price attribution remain exact.
                model=model,
                temperature=0,
                max_tokens=self.max_tokens,
            )
            if telemetry is not None:
                telemetry.record(response)
            summary = (response.content or "").strip()
            if not summary:
                logger.warning("Active context compaction returned an empty summary")
                return messages
        except Exception as exc:
            logger.error("Active context compaction failed: {}", exc)
            return messages

        return [
            *messages[:leading_system_count],
            {
                "role": "user",
                "content": f"[Compacted earlier context]\n\n{summary}",
            },
            {
                "role": "assistant",
                "content": "Understood. I will continue from this compacted context.",
            },
            *messages[cut_at:],
        ]

    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
                execution_key = msg.execution_key
                if msg.metadata.get("control") == "cancel":
                    await self._handle_inbound_control(msg)
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content="",
                            run_id=msg.run_id,
                            conversation_id=msg.conversation_id,
                            metadata={"control": "cancelled"},
                        )
                    )
                    self._pending_cancellations.discard(execution_key)
                    continue

                if msg.metadata.get("control") in {"steer", "steer_status"}:
                    message_task = self._active_message_tasks.get(execution_key)
                    await self.steering.handle_control(
                        msg,
                        run_active=bool(message_task and not message_task.done()),
                    )
                    continue

                queue = self._message_queues.setdefault(execution_key, asyncio.Queue())
                queue.put_nowait(msg)
                message_task = self._active_message_tasks.get(execution_key)
                if not message_task or message_task.done():
                    message_task = self._track_task(self._process_chat_queue(execution_key))
                    self._active_message_tasks[execution_key] = message_task
                    if execution_key in self._pending_cancellations:
                        self._pending_cancellations.discard(execution_key)
                        message_task.cancel()
            except asyncio.TimeoutError:
                continue

    async def _process_chat_queue(self, execution_key: str) -> None:
        """Process messages for one execution while other runs proceed independently."""
        current_task = asyncio.current_task()
        queue = self._message_queues[execution_key]
        try:
            while not queue.empty():
                msg = queue.get_nowait()
                try:
                    await self._process_and_publish_message(msg)
                finally:
                    queue.task_done()
                if current_task and current_task.cancelling():
                    break
        finally:
            await self.steering.reject_pending(execution_key)
            if current_task and current_task.cancelling():
                while not queue.empty():
                    queue.get_nowait()
                    queue.task_done()
            if self._active_message_tasks.get(execution_key) is current_task:
                self._active_message_tasks.pop(execution_key, None)
            self._message_queues.pop(execution_key, None)

    async def _process_and_publish_message(self, msg: InboundMessage) -> None:
        """Process one chat without blocking the global inbound consumer."""
        try:
            session_lock = self._session_locks.setdefault(
                msg.session_key,
                asyncio.Lock(),
            )
            async with session_lock:
                async with self._message_semaphore:
                    response = await self._process_message(msg)
                    if response:
                        if not response.metadata.get("control"):
                            await self.bus.publish_outbound(
                                OutboundMessage(
                                    channel=response.channel,
                                    chat_id=response.chat_id,
                                    content="Send Message",
                                    run_id=response.run_id,
                                    conversation_id=response.conversation_id,
                                    metadata={
                                        "intermediate": True,
                                        "phase": "message",
                                        "terminal_action": "send_message",
                                    },
                                )
                            )
                        await self.bus.publish_outbound(response)
        except asyncio.CancelledError:
            logger.info("Cancelled active chat run {}", msg.execution_key)
        except Exception as e:
            logger.exception(f"Error processing message: {e}")
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=f"Sorry, I encountered an error: {str(e)}",
                    run_id=msg.run_id,
                    conversation_id=msg.conversation_id,
                )
            )

    async def close_mcp(self) -> None:
        """Close MCP connections."""
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> OutboundMessage | None:
        """
        Process a single inbound message.

        Args:
            msg: The inbound message to process.
            session_key: Override session key (used by process_direct).
            on_progress: Optional callback for intermediate output (defaults to bus publish).

        Returns:
            The response message, or None if no response needed.
        """
        request_credential = self.request_router.take_request_credential(msg)
        control_response = await self.request_router.handle_control(msg, request_credential)
        if control_response is not None:
            return control_response

        # System messages route back via chat_id ("channel:chat_id")
        if msg.channel == "system":
            return await self._process_system_message(msg)

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info(f"Processing message from {msg.channel}:{msg.sender_id}: {preview}")

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)
        if self.tool_profile != "full":
            self._clear_composio_api_key_pending(session)
        elif (
            session.metadata.get(_AWAITING_COMPOSIO_API_KEY)
            and self._composio_credentials_path().exists()
        ):
            self._clear_composio_api_key_pending(session)

        # Handle slash commands
        cmd = msg.content.strip().lower()
        if cmd == "/new":
            # Capture events before clearing (avoid race condition with background task)
            events_to_archive = session.events.copy()
            session.clear()
            session.metadata.pop("active_analysis_id", None)
            session.metadata.pop("answer_artifacts", None)
            self.sessions.save(session)
            self.sessions.invalidate(session.key)

            async def _consolidate_and_cleanup():
                temp_session = Session(key=session.key)
                temp_session.events = events_to_archive
                await self._consolidate_memory(temp_session, archive_all=True)

            if self.tool_profile != "trusted-analysis":
                self._track_task(_consolidate_and_cleanup())
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=(
                    "New session started."
                    if self.tool_profile == "trusted-analysis"
                    else "New session started. Memory consolidation in progress."
                ),
            )
        if cmd == "/help":
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="🐈 analyst_runtime commands:\n/new — Start a new conversation\n/help — Show available commands\n/config — Configure settings (model, etc.)\n/upgrade — Restart your production sandbox (gateway-managed)",
            )

        if cmd == "/upgrade":
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="Use /upgrade from the Telegram bot in production to restart your sandbox onto the latest runtime.",
            )

        if cmd == "/whatcanyoudo":
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=self._build_capability_card(),
                metadata={
                    "inline_keyboard": [
                        [
                            {
                                "text": "Send an email",
                                "callback_data": "action:Help me write and send an email",
                            }
                        ],
                        [
                            {
                                "text": "Search the web",
                                "callback_data": "action:Search the web for something",
                            }
                        ],
                        [
                            {
                                "text": "Set a reminder",
                                "callback_data": "action:Set a reminder for me",
                            }
                        ],
                    ]
                },
            )

        if cmd == "/whatsapp":
            from analyst_runtime.utils.helpers import get_data_path

            status_path = get_data_path() / "whatsapp" / "status.json"
            try:
                state = (
                    json.loads(status_path.read_text()).get("state", "disconnected")
                    if status_path.exists()
                    else "disconnected"
                )
            except Exception:
                state = "disconnected"

            # status.json persists across container restarts. Trust it only when
            # a WhatsApp channel is actually running in this process right now.
            wa = self.channel_manager.get_channel("whatsapp") if self.channel_manager else None
            if state in ("connected", "qr_pending") and not (wa and wa.is_running):
                logger.info(
                    "WhatsApp status.json says '{}' but no live channel — treating as disconnected",
                    state,
                )
                state = "disconnected"

            if state == "connected":
                text = "✅ **WhatsApp connected.**\n\nYou can message Samantha on WhatsApp."
            elif state == "qr_pending":
                text = (
                    "⏳ **Waiting for QR scan.**\n\n"
                    "Check the QR image I sent above and scan it in WhatsApp:\n"
                    "**Settings → Linked Devices → Link a Device**"
                )
            else:
                started = False
                if self.channel_manager is not None:
                    from analyst_runtime.channels.whatsapp import WhatsAppChannel

                    if wa is None:
                        # WhatsApp not in config — create and register on demand
                        from analyst_runtime.config.schema import WhatsAppConfig

                        wa = WhatsAppChannel(
                            WhatsAppConfig(enabled=True),
                            self.bus,
                            owner_chat_id=msg.chat_id,
                        )
                        self.channel_manager.channels["whatsapp"] = wa
                        self._track_task(self.channel_manager._start_channel("whatsapp", wa))
                    if hasattr(wa, "start_bridge"):
                        started = await wa.start_bridge()
                if started:
                    text = (
                        "🔄 **Starting WhatsApp bridge...**\n\n"
                        "A QR code will appear here in ~10 seconds.\n"
                        "Scan it in WhatsApp: **Settings → Linked Devices → Link a Device**"
                    )
                else:
                    text = (
                        "📵 **WhatsApp bridge is not running.**\n\n"
                        "Start the bridge on the server:\n"
                        "`analyst_runtime channels login`\n\n"
                        "I'll send you a QR code automatically once it's ready to scan."
                    )
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=text)

        if cmd.startswith("__config:model:"):
            model_id = msg.content.strip()[len("__config:model:") :]
            if model_id == "default":
                session.metadata.pop("model", None)
            else:
                session.metadata["model"] = model_id
            self.sessions.save(session)
            return None  # TelegramChannel already confirmed via edit_message_text

        # The production trusted-analysis profile must not launch model calls
        # outside the request telemetry returned to the Web usage ledger.
        if (
            self.tool_profile != "trusted-analysis"
            and len(session.get_consolidation_events()) > self.memory_window
        ):
            self._track_task(self._consolidate_memory(session))

        request_uuid = str(uuid.uuid4())
        self._set_tool_context(
            msg.channel,
            msg.chat_id,
            session_key=key,
            user_message=msg.content,
            inbound_turn_id=request_uuid,
            analysis_conversation_id=msg.conversation_id,
            run_id=msg.run_id,
        )

        # Capture history BEFORE emitting the current user_input event.
        # build_messages appends current_message separately — including the
        # just-added user_input in the history snapshot would send the message
        # twice to the LLM.
        history_context: dict[str, Any] = {}
        history_snapshot = session.get_history(
            max_messages=self.memory_window,
            context=history_context,
        )
        runtime_provenance = self._runtime_provenance()
        confirmed_question = None
        if self.request_router.is_trusted_gateway(msg):
            confirmed_question = self._remember_confirmed_semantics(msg.content, history_snapshot)
        analysis_question = confirmed_question or msg.content
        confirmed_semantics_sha256 = (
            self._confirmed_semantics_sha256(analysis_question) or "unavailable"
        )

        # Emit user_input event. The index scopes exported trace telemetry to this turn.
        turn_event_start = len(session.events)
        session.add_event(
            {
                "uuid": request_uuid,
                "parent_uuid": None,
                "type": "user_input",
                "content": msg.content,
                "channel": msg.channel,
                "chat_id": msg.chat_id,
                "media": msg.media if msg.media else None,
            }
        )

        answer_key = self.analysis_artifacts.completed_answer_key(
            analysis_question,
            runtime_provenance["workspace_data_manifest_sha256"],
            confirmed_semantics_sha256,
        )
        answer_artifacts = session.metadata.get("answer_artifacts")
        if isinstance(answer_artifacts, dict) and isinstance(answer_artifacts.get(answer_key), str):
            session.metadata["active_analysis_id"] = answer_artifacts[answer_key]
        analysis_instruction, active_analysis_available = self._reuse_analysis_context(
            session,
            parent_uuid=request_uuid,
            question=analysis_question,
            data_manifest_sha256=runtime_provenance["workspace_data_manifest_sha256"],
            confirmed_semantics_sha256=confirmed_semantics_sha256,
        )
        active_analysis_id = session.metadata.get("active_analysis_id")
        exact_artifact_reuse = bool(
            active_analysis_available
            and isinstance(active_analysis_id, str)
            and active_analysis_id.startswith("chat-answer:")
        )
        reuse_requested = active_analysis_available and (
            exact_artifact_reuse or self._explicit_analysis_reuse(msg.content)
        )
        followup_analysis = reuse_requested and self._explicit_followup_analysis(msg.content)
        interpretation_only = reuse_requested and not followup_analysis

        bootstrap_instruction = self._composio_bootstrap_instruction(session, msg.content)
        initial_messages = self.context.build_messages(
            history=history_snapshot,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
        )
        self._append_system_instruction(
            initial_messages,
            self._confirmed_semantics_reuse_instruction(analysis_question),
        )
        self._append_system_instruction(
            initial_messages,
            self._semantic_clarification_instruction(msg.content, history_snapshot),
        )
        self._append_system_instruction(initial_messages, bootstrap_instruction)
        self._append_system_instruction(initial_messages, analysis_instruction)
        if interpretation_only:
            self._append_system_instruction(
                initial_messages,
                "The user explicitly asked to keep the prior analysis scope and result "
                "unchanged without authorizing new evidence access. This is an "
                "interpretation-only turn: answer now from the active approved analysis and "
                "explicitly restate its net-loss result. No tools are available. Do not "
                "announce future data access, recalculation, or investigation.",
            )
        elif followup_analysis:
            self._append_system_instruction(
                initial_messages,
                "The user explicitly asked to keep the prior analysis scope and result "
                "unchanged. Keep the active analysis unchanged as the numeric baseline and "
                "explicitly restate its net-loss result. Do not rerun or replace that approved "
                "calculation. Tools remain available for this new follow-up analysis. Perform "
                "a bounded, targeted investigation against the fixed population: use the exact "
                "approved artifact path and evidence locators, do not enumerate unrelated "
                "analysis artifacts, install packages, or scan every workspace file. Conclude "
                "in this turn with recorded associations, inferences, hypotheses, and explicit "
                "limitations; do not merely announce future investigation.",
            )
        self._append_system_instruction(
            initial_messages,
            self.request_router.trusted_system_instruction(msg),
        )

        # Emit prompt_snapshot (system prompt assembled for this turn)
        system_content = initial_messages[0].get("content", "") if initial_messages else ""
        if not isinstance(system_content, str):
            system_content = json.dumps(system_content)
        session.add_event(
            {
                "uuid": str(uuid.uuid4()),
                "parent_uuid": request_uuid,
                "type": "prompt_snapshot",
                "content": system_content,
                "tools": self.tools.get_definitions() if not interpretation_only else [],
                "context": {
                    "history": history_context,
                    "input": self._message_context_manifest(initial_messages),
                },
            }
        )

        async def _bus_progress(
            text: str | None,
            tool: ProgressTool | str | None = None,
        ) -> None:
            if not text and not tool:
                return
            tool_payload = tool if isinstance(tool, dict) else None
            tool_name = tool_payload.get("name") if tool_payload else str(tool or "")
            public_text = text or ""
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=public_text,
                    run_id=msg.run_id,
                    conversation_id=msg.conversation_id,
                    metadata={
                        **(msg.metadata or {}),
                        "intermediate": True,
                        "phase": "tool" if tool else "thinking",
                        "tool": tool_payload,
                        "tool_hint": tool_name,
                    },
                )
            )

        try:
            routing = self.request_router.resolve_run(msg, request_credential)
        except RoutingError as error:
            error_code = (
                self.MODEL_PROFILE_ERROR_CODE
                if error.kind == "model_profile"
                else self.MODEL_CREDENTIAL_ERROR_CODE
            )
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=self._support_error_message(error_code),
                run_id=msg.run_id,
                conversation_id=msg.conversation_id,
                metadata={"error_code": error_code},
            )
        trusted_profile = routing.profile
        trusted_model = trusted_profile.model if trusted_profile else None
        # Model selection is frozen for this run and must not mutate the
        # conversation session's configured default.
        active_model = trusted_model or session.metadata.get("model") or self.model

        # Migrate stale model IDs stored before the bedrock/moonshotai prefix fix.
        # kimi-k2.5 used to be stored as "kimi-k2.5" (routes to Moonshot direct API,
        # no key configured) — upgrade to the correct Bedrock path.
        model_migrations = {
            "kimi-k2.5": "bedrock/moonshotai.kimi-k2.5",
        }
        if active_model in model_migrations:
            migrated = model_migrations[active_model]
            logger.warning(
                "Migrating stale model ID %r → %r for session %s",
                active_model,
                migrated,
                key,
            )
            session.metadata["model"] = migrated
            active_model = migrated

        with self.request_router.bind_provider(routing):
            loop_result = await self._run_agent_loop(
                initial_messages,
                on_progress=on_progress or _bus_progress,
                session=session,
                request_uuid=request_uuid,
                model=active_model,
                execution_key=msg.execution_key,
                allow_tools=not interpretation_only,
            )
        final_content, tools_used = loop_result

        terminal_error_code = loop_result.error_code
        if final_content is None:
            if terminal_error_code is None:
                terminal_error_code = (
                    self.ITERATION_LIMIT_ERROR_CODE
                    if loop_result.terminal_reason == "iteration_limit"
                    else self.TOOL_PROTOCOL_ERROR_CODE
                )
            final_content = self._support_error_message(terminal_error_code)
            # Emit final_response for max-iterations case (loop emits it on normal break)
            session.add_event(
                {
                    "uuid": str(uuid.uuid4()),
                    "parent_uuid": request_uuid,
                    "type": "final_response",
                    "message_id": "",
                    "model": active_model,
                    "content": final_content,
                    "reasoning": None,
                    "usage": {},
                    "tools_used": tools_used if tools_used else None,
                    "iterations": loop_result.iterations,
                }
            )

        if terminal_error_code:
            logger.error(f"Response to {msg.channel}:{msg.sender_id}: {final_content}")
        else:
            preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
            logger.info(f"Response to {msg.channel}:{msg.sender_id}: {preview}")

        turn_events = session.events[turn_event_start:]
        tool_events = [event for event in turn_events if event.get("type") == "tool_result"]
        current_analysis_id = session.metadata.get("active_analysis_id")
        if (
            not terminal_error_code
            and self.request_router.is_trusted_gateway(msg)
            and tools_used
            and tool_events
            and all(event.get("status") == "completed" for event in tool_events)
            and self._confirmed_semantics_reuse_instruction(analysis_question)
            and runtime_provenance["workspace_data_manifest_sha256"] != "unavailable"
            and (
                current_analysis_id is None
                or str(current_analysis_id).startswith("chat-answer:")
            )
        ):
            analysis_id = self.analysis_artifacts.save_completed_answer(
                question=analysis_question,
                answer=final_content,
                data_manifest_sha256=runtime_provenance[
                    "workspace_data_manifest_sha256"
                ],
                confirmed_semantics_sha256=confirmed_semantics_sha256,
                tools_used=tools_used,
                evidence=[
                    {
                        "tool_name": event.get("tool_name"),
                        "input_sha256": self._content_sha256(event.get("tool_input")),
                        "output_sha256": self._content_sha256(event.get("content")),
                    }
                    for event in tool_events
                ],
            )
            session.metadata["active_analysis_id"] = analysis_id
            answer_artifacts = session.metadata.get("answer_artifacts")
            if not isinstance(answer_artifacts, dict):
                answer_artifacts = {}
                session.metadata["answer_artifacts"] = answer_artifacts
            answer_artifacts[answer_key] = analysis_id
            session.add_event(
                {
                    "uuid": str(uuid.uuid4()),
                    "parent_uuid": request_uuid,
                    "type": "analysis_context",
                    "mode": "created",
                    "analysis_id": analysis_id,
                }
            )

        if (
            session.metadata.get(_AWAITING_COMPOSIO_API_KEY)
            and self._composio_credentials_path().exists()
        ):
            self._clear_composio_api_key_pending(session)

        self._merge_persisted_session_metadata(session)
        self.sessions.save(session)

        # Trigger async history compression if the session has grown past the threshold.
        after, keep = self._session_compress_config.get(
            key, (self.compress_after_turns, self.compress_keep_turns)
        )
        if self.tool_profile != "trusted-analysis" and session.count_turns() > after:
            self._track_task(self._compress_history(key, keep_turns=keep))

        # Extract action chips for Telegram (strips <!-- CHIPS: [...] --> from content)
        outbound_metadata = dict(msg.metadata or {})
        outbound_metadata["usage"] = {
            **loop_result.usage,
            "application_retry_count": loop_result.application_retry_count,
            "model_call_count": loop_result.model_call_count,
            "provider_retry_count": loop_result.provider_retry_count,
            "retry_count": loop_result.retry_count,
        }
        outbound_metadata["model"] = active_model
        outbound_metadata["runtime_provenance"] = {
            **runtime_provenance,
            "model_provider": trusted_profile.provider if trusted_profile else "unavailable",
        }
        outbound_metadata["trace_summary"] = self._trace_summary(session.events[turn_event_start:])
        if terminal_error_code:
            outbound_metadata["error_code"] = terminal_error_code
        if msg.channel == "telegram":
            final_content, chips = self._extract_action_chips(final_content)
            if chips:
                outbound_metadata["inline_keyboard"] = chips
                logger.debug("Action chips generated: {} chips", len(chips))

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            run_id=msg.run_id,
            conversation_id=msg.conversation_id,
            metadata=outbound_metadata,
        )

    async def _process_system_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a system message (e.g., subagent announce).

        The chat_id field contains "original_channel:original_chat_id" to route
        the response back to the correct destination.
        """
        logger.info(f"Processing system message from {msg.sender_id}")

        # Parse origin from chat_id (format: "channel:chat_id")
        if ":" in msg.chat_id:
            parts = msg.chat_id.split(":", 1)
            origin_channel = parts[0]
            origin_chat_id = parts[1]
        else:
            # Fallback
            origin_channel = "cli"
            origin_chat_id = msg.chat_id

        session_key = f"{origin_channel}:{origin_chat_id}"
        session = self.sessions.get_or_create(session_key)
        self._set_tool_context(
            origin_channel,
            origin_chat_id,
            session_key=session_key,
            user_message=None,
            inbound_turn_id=None,
        )

        # Capture history before emitting user_input to avoid duplicate message in LLM context.
        history_snapshot = session.get_history(max_messages=self.memory_window)

        # Emit user_input event for system message
        request_uuid = str(uuid.uuid4())
        session.add_event(
            {
                "uuid": request_uuid,
                "parent_uuid": None,
                "type": "user_input",
                "content": f"[System: {msg.sender_id}] {msg.content}",
                "channel": "system",
                "chat_id": msg.chat_id,
                "media": None,
            }
        )

        initial_messages = self.context.build_messages(
            history=history_snapshot,
            current_message=msg.content,
            channel=origin_channel,
            chat_id=origin_chat_id,
        )

        # Emit prompt_snapshot
        system_content = initial_messages[0].get("content", "") if initial_messages else ""
        if not isinstance(system_content, str):
            system_content = json.dumps(system_content)
        session.add_event(
            {
                "uuid": str(uuid.uuid4()),
                "parent_uuid": request_uuid,
                "type": "prompt_snapshot",
                "content": system_content,
                "tools": self.tools.get_definitions(),
            }
        )

        final_content, _ = await self._run_agent_loop(
            initial_messages,
            session=session,
            request_uuid=request_uuid,
        )

        if final_content is None:
            final_content = "Background task completed."
            session.add_event(
                {
                    "uuid": str(uuid.uuid4()),
                    "parent_uuid": request_uuid,
                    "type": "final_response",
                    "message_id": "",
                    "model": self.model,
                    "content": final_content,
                    "reasoning": None,
                    "usage": {},
                    "tools_used": None,
                    "iterations": self.max_iterations,
                }
            )

        self._merge_persisted_session_metadata(session)
        self.sessions.save(session)

        after, keep = self._session_compress_config.get(
            session_key, (self.compress_after_turns, self.compress_keep_turns)
        )
        if self.tool_profile != "trusted-analysis" and session.count_turns() > after:
            self._track_task(self._compress_history(session_key, keep_turns=keep))

        return OutboundMessage(
            channel=origin_channel, chat_id=origin_chat_id, content=final_content
        )

    async def _consolidate_memory(self, session, archive_all: bool = False) -> None:
        """Consolidate old messages into MEMORY.md + HISTORY.md.

        Args:
            archive_all: If True, clear all messages and reset session (for /new command).
                       If False, only write to files without modifying session.
        """
        async with self._consolidation_lock:
            await self._consolidate_memory_inner(session, archive_all)

    async def _consolidate_memory_inner(self, session, archive_all: bool = False) -> None:
        """Inner consolidation logic, must be called under _consolidation_lock."""
        memory = self.context.memory
        consolidation_events = session.get_consolidation_events()

        if not archive_all and self.consolidation_interval > 0:
            now = asyncio.get_event_loop().time()
            last = self._last_consolidation_time.get(session.key, 0.0)
            elapsed = now - last
            if elapsed < self.consolidation_interval:
                logger.debug(
                    "Consolidation skipped for {}: {:.0f}s elapsed < {}s interval",
                    session.key,
                    elapsed,
                    self.consolidation_interval,
                )
                return

        if archive_all:
            old_events = consolidation_events
            keep_count = 0
            logger.info(
                f"Memory consolidation (archive_all): {len(consolidation_events)} consolidation events archived"
            )
        else:
            keep_count = self.memory_window // 2
            if len(consolidation_events) <= keep_count:
                logger.debug(
                    f"Session {session.key}: No consolidation needed (consolidation_events={len(consolidation_events)}, keep={keep_count})"
                )
                return

            events_to_process = len(consolidation_events) - session.last_consolidated
            if events_to_process <= 0:
                logger.debug(
                    f"Session {session.key}: No new events to consolidate (last_consolidated={session.last_consolidated}, total={len(consolidation_events)})"
                )
                return

            old_events = (
                consolidation_events[session.last_consolidated : -keep_count]
                if keep_count > 0
                else consolidation_events[session.last_consolidated :]
            )
            if not old_events:
                return
            logger.info(
                f"Memory consolidation started: {len(consolidation_events)} total, {len(old_events)} new to consolidate, {keep_count} keep"
            )

        lines = []
        for e in old_events:
            t = e.get("type")
            if t == "user_input" and e.get("content"):
                lines.append(f"[{e.get('timestamp', '?')[:16]}] USER: {e['content']}")
            elif t == "final_response" and e.get("content"):
                tools = f" [tools: {', '.join(e['tools_used'])}]" if e.get("tools_used") else ""
                lines.append(f"[{e.get('timestamp', '?')[:16]}] ASSISTANT{tools}: {e['content']}")
        conversation = "\n".join(lines)
        current_memory = memory.read_long_term()

        prompt = f"""You are a memory consolidation agent. Process this conversation and return a JSON object with exactly two keys:

1. "history_entry": A paragraph (2-5 sentences) summarizing the key events/decisions/topics. Start with a timestamp like [YYYY-MM-DD HH:MM]. Include enough detail to be useful when found by grep search later.

2. "memory_update": The updated long-term memory content. Add any new facts: user location, preferences, personal info, habits, project context, technical decisions, tools/services used. If nothing new, return the existing content unchanged.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{conversation}

Respond with ONLY valid JSON, no markdown fences."""

        try:
            response = await self.provider.chat(
                messages=[
                    {
                        "role": "system",
                        "content": "You are a memory consolidation agent. Respond only with valid JSON.",
                    },
                    {"role": "user", "content": prompt},
                ],
                model=self.consolidation_model or self.model,
            )
            text = (response.content or "").strip()
            if not text:
                logger.warning("Memory consolidation: LLM returned empty response, skipping")
                return
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            result = json_repair.loads(text)
            if not isinstance(result, dict):
                logger.warning(
                    f"Memory consolidation: unexpected response type, skipping. Response: {text[:200]}"
                )
                return

            if entry := result.get("history_entry"):
                if not isinstance(entry, str):
                    entry = json.dumps(entry, ensure_ascii=False)
                memory.append_history(entry)
            if update := result.get("memory_update"):
                if not isinstance(update, str):
                    update = json.dumps(update, indent=2, ensure_ascii=False)
                if update != current_memory:
                    memory.write_long_term(update)

            if archive_all:
                session.last_consolidated = 0
            else:
                session.last_consolidated = len(consolidation_events) - keep_count
                self._last_consolidation_time[session.key] = asyncio.get_event_loop().time()
            logger.info(
                f"Memory consolidation done: {len(consolidation_events)} consolidation events, last_consolidated={session.last_consolidated}"
            )
        except Exception as e:
            logger.error(f"Memory consolidation failed: {e}")
            # Notify the user that memory may be impaired
            try:
                if ":" in session.key:
                    ch, cid = session.key.split(":", 1)
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=ch,
                            chat_id=cid,
                            content="Warning: memory consolidation failed — long-term memory may be impaired for this session.",
                        )
                    )
            except Exception:
                pass  # Best-effort notification

    async def _compress_history(self, session_key: str, keep_turns: int) -> None:
        """Summarise old conversation turns and replace them with a single summary event.

        Runs asynchronously under _consolidation_lock (shared with memory consolidation)
        so it never races with another consolidation or compression task.

        Uses session_key (not the session object) so it always works with the latest
        cached version, even if another turn was processed while this task was queued.
        """
        async with self._consolidation_lock:
            session = self.sessions.get_or_create(session_key)
            turn_count = session.count_turns()
            if turn_count <= keep_turns:
                return  # Already compact enough (another task may have beaten us)

            # Gather old events: everything before the keep_turns window.
            user_input_indices = [
                i for i, e in enumerate(session.events) if e.get("type") == "user_input"
            ]
            cut_at = user_input_indices[-keep_turns]
            old_events = session.events[:cut_at]

            # Build a readable transcript of the old turns for the summariser.
            lines: list[str] = []
            for e in old_events:
                t = e.get("type")
                if t == HISTORY_SUMMARY_TYPE:
                    lines.insert(0, f"[Prior Summary]\n{e.get('content', '')}\n")
                elif t == "user_input" and e.get("content"):
                    lines.append(f"[{e.get('timestamp', '?')[:16]}] USER: {e['content']}")
                elif t == "final_response" and e.get("content"):
                    tools = f" [tools: {', '.join(e['tools_used'])}]" if e.get("tools_used") else ""
                    lines.append(
                        f"[{e.get('timestamp', '?')[:16]}] ASSISTANT{tools}: {e['content']}"
                    )

            if not lines:
                return

            conversation = "\n".join(lines)
            prompt = (
                "You are summarising a conversation history to compress it for an AI agent's context window.\n"
                "Write a concise summary (150-300 words) capturing:\n"
                "- Key topics and decisions discussed\n"
                "- Important user facts, preferences, or context revealed\n"
                "- Any tasks completed or still in progress\n"
                "- Emotional tone or relationship context where relevant\n\n"
                "The summary will be shown to the agent in place of the raw conversation.\n"
                "Be specific enough to preserve useful context.\n\n"
                f"## Conversation to Summarise\n\n{conversation}\n\n"
                "Write ONLY the summary text, no preamble or labels."
            )

            try:
                response = await self.provider.chat(
                    messages=[{"role": "user", "content": prompt}],
                    model=self.consolidation_model or self.model,
                )
                summary_text = (response.content or "").strip()
                if not summary_text:
                    logger.warning(
                        "History compression for {}: LLM returned empty response", session_key
                    )
                    return

                session.compress_events(summary_text, keep_turns=keep_turns)
                self.sessions.save(session)
                logger.info(
                    "History compression done for {}: {} turns → summary + {} kept ({} events)",
                    session_key,
                    turn_count,
                    keep_turns,
                    len(session.events),
                )
            except Exception as e:
                logger.error("History compression failed for {}: {}", session_key, e)

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: ProgressCallback | None = None,
    ) -> str:
        """
        Process a message directly (for CLI or cron usage).

        Args:
            content: The message content.
            session_key: Session identifier (overrides channel:chat_id for session lookup).
            channel: Source channel (for tool context routing).
            chat_id: Source chat ID (for tool context routing).
            on_progress: Optional callback for intermediate output.

        Returns:
            The agent's response.
        """
        await self._connect_mcp()
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)

        response = await self._process_message(
            msg, session_key=session_key, on_progress=on_progress
        )
        return response.content if response else ""
