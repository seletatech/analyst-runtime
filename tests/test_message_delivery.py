import json

from analyst_runtime.agent.tools.message import MessageTool
from analyst_runtime.bus.events import OutboundMessage


async def test_web_message_tool_marks_delivery_as_intermediate() -> None:
    delivered: list[OutboundMessage] = []

    async def send(message: OutboundMessage) -> None:
        delivered.append(message)
        message.delivery.succeed(
            {
                "attachments": [
                    {"name": "result.csv", "status": "delivered"},
                ]
            }
        )

    tool = MessageTool(
        send_callback=send,
        default_channel="web",
        default_chat_id="chat-run-123",
    )
    result = await tool.execute(
        content="文件已生成。",
        media=["artifacts/result.csv"],
    )

    assert json.loads(result) == {
        "attachments": [{"name": "result.csv", "status": "delivered"}],
        "delivered": True,
    }
    assert delivered[0].media == ["artifacts/result.csv"]
    assert delivered[0].metadata == {
        "intermediate": True,
        "phase": "delivery",
    }


async def test_web_message_tool_reports_the_real_attachment_delivery_failure() -> None:
    async def send(message: OutboundMessage) -> None:
        message.delivery.fail(
            RuntimeError("ANALYST-RUNTIME-ATTACHMENT-001: report.html was rejected")
        )

    tool = MessageTool(
        send_callback=send,
        default_channel="web",
        default_chat_id="chat-run-123",
    )

    result = await tool.execute(
        content="文件已生成。",
        media=["artifacts/report.html", "artifacts/result.csv"],
    )

    assert "ANALYST-RUNTIME-ATTACHMENT-001" in result
    assert "Message sent" not in result
