from typing import List

from typing_extensions import override

from mentat.command.command import Command, CommandArgument
from mentat.session_context import SESSION_CONTEXT


class ClearCommand(Command, command_name="clear"):
    @override
    async def apply(self, *args: str) -> None:
        session_context = SESSION_CONTEXT.get()
        stream = session_context.stream
        conversation = session_context.conversation
        code_context = session_context.code_context

        conversation.clear_messages()
        code_context.clear_auto_context()
        code_context.refresh_context_display()
        message = "Message history cleared"
        stream.send(message, style="success")

    @override
    @classmethod
    def arguments(cls) -> List[CommandArgument]:
        return []

    @override
    @classmethod
    def argument_autocompletions(
        cls, arguments: list[str], argument_position: int
    ) -> list[str]:
        return []

    @override
    @classmethod
    def help_message(cls) -> str:
        return (
            "Clear the current message history and auto included code features from"
            " context."
        )
