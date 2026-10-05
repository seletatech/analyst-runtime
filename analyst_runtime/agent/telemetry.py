"""Per-run model usage and retry accounting."""

from dataclasses import dataclass, field

from analyst_runtime.providers.base import LLMResponse


@dataclass
class RunModelTelemetry:
    """Accumulate provider and application retries without retaining prompts."""

    model_call_count: int = 0
    provider_retry_count: int = 0
    application_retry_count: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    complete_returned_usage: bool = True

    def record(self, response: LLMResponse) -> None:
        retries = max(0, response.retry_count)
        self.model_call_count += 1 + retries
        self.provider_retry_count += retries
        self.complete_returned_usage &= (
            response.finish_reason != "error" and retries == 0
            and all(
                isinstance(response.usage.get(key), int)
                and not isinstance(response.usage.get(key), bool)
                and response.usage[key] >= 0
                for key in ("prompt_tokens", "completion_tokens")
            )
        )
        for key, value in response.usage.items():
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                self.usage[key] = self.usage.get(key, 0) + value

    def record_application_retry(self) -> None:
        self.application_retry_count += 1

    @property
    def retry_count(self) -> int:
        return self.provider_retry_count + self.application_retry_count
