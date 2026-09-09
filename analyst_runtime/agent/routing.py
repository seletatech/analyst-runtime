"""Trusted gateway request routing and request-scoped provider binding."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Literal

from analyst_runtime.bus.events import InboundMessage, OutboundMessage
from analyst_runtime.providers.base import LLMProvider
from analyst_runtime.providers.registry import ModelProfile, resolve_model_profile
from analyst_runtime.workspace import WorkspaceConfiguration

RoutingErrorKind = Literal["model_profile", "provider_credential"]


class RoutingError(ValueError):
    """A trusted request could not be bound to a safe Runtime route."""

    def __init__(self, kind: RoutingErrorKind) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class RequestCredential:
    """Validated request-scoped BYOK credential."""

    api_key: str
    provider: str


@dataclass(frozen=True)
class RunRouting:
    """Validated model profile and optional request-scoped credential."""

    profile: ModelProfile | None
    credential: RequestCredential | None


class RuntimeRequestRouter:
    """Validate trusted gateway metadata before it reaches the agent loop."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        workspace_configuration: WorkspaceConfiguration,
    ) -> None:
        self.provider = provider
        self.workspace_configuration = workspace_configuration

    def is_trusted_gateway(self, msg: InboundMessage) -> bool:
        trusted_gateway = self.workspace_configuration.trusted_gateway
        return bool(
            trusted_gateway is not None
            and msg.channel == "web"
            and msg.metadata.get("runtime") == trusted_gateway.runtime
            and msg.metadata.get("project_id") == trusted_gateway.project_id
        )

    def take_request_credential(self, msg: InboundMessage) -> dict[str, Any] | None:
        """Remove secret transport metadata before logging or persistence."""
        credential = msg.metadata.pop("_provider_credential", None)
        if not self.is_trusted_gateway(msg) or not isinstance(credential, dict):
            return None
        return credential

    def trusted_system_instruction(self, msg: InboundMessage) -> str | None:
        if not self.is_trusted_gateway(msg):
            return None
        instruction = msg.metadata.get("trusted_system_instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            return None
        return instruction.strip()

    async def handle_control(
        self,
        msg: InboundMessage,
        credential: dict[str, Any] | None,
    ) -> OutboundMessage | None:
        control = msg.metadata.get("control")
        if control == "verify_provider_credential":
            verified = False
            if credential is not None:
                api_key = credential.get("api_key")
                provider_name = credential.get("provider")
                if isinstance(api_key, str) and isinstance(provider_name, str):
                    verified = await self.provider.verify_request_credentials(
                        api_key=api_key,
                        provider=provider_name,
                    )
            return self._control_message(
                msg,
                control="provider_credential_verified",
                verified=verified,
            )

        if control != "resolve_model_profile" or not self.is_trusted_gateway(msg):
            return None
        profile_id = msg.metadata.get("model_profile_id")
        try:
            profile = resolve_model_profile(str(profile_id))
        except ValueError:
            return self._control_message(
                msg,
                control="model_profile_rejected",
                model_profile_id=str(profile_id),
            )
        return self._control_message(
            msg,
            control="model_profile_resolved",
            model=profile.model,
            model_profile_id=profile.id,
            provider=profile.provider,
        )

    def resolve_run(
        self,
        msg: InboundMessage,
        credential: dict[str, Any] | None,
    ) -> RunRouting:
        if not self.is_trusted_gateway(msg):
            return RunRouting(profile=None, credential=None)

        profile = None
        profile_id = msg.metadata.get("model_profile_id")
        if isinstance(profile_id, str):
            try:
                profile = resolve_model_profile(profile_id)
            except ValueError:
                profile = None
        if profile is None:
            raise RoutingError("model_profile")
        parsed_credential = None
        if credential is not None:
            api_key = credential.get("api_key")
            provider = credential.get("provider")
            if (
                not isinstance(api_key, str)
                or not api_key.strip()
                or not isinstance(provider, str)
                or provider != profile.provider
            ):
                raise RoutingError("provider_credential")
            parsed_credential = RequestCredential(api_key=api_key, provider=provider)
        return RunRouting(profile=profile, credential=parsed_credential)

    @contextmanager
    def bind_provider(self, routing: RunRouting) -> Iterator[None]:
        token = None
        if routing.credential is not None and routing.profile is not None:
            token = self.provider.set_request_credentials(
                api_key=routing.credential.api_key,
                provider=routing.credential.provider,
            )
        elif routing.profile is not None:
            token = self.provider.set_request_provider(provider=routing.profile.provider)
        try:
            yield
        finally:
            self.provider.reset_request_credentials(token)

    @staticmethod
    def _control_message(
        msg: InboundMessage,
        *,
        control: str,
        **metadata: Any,
    ) -> OutboundMessage:
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content="",
            run_id=msg.run_id,
            conversation_id=msg.conversation_id,
            metadata={"control": control, **metadata},
        )
