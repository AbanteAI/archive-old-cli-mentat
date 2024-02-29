import logging
import shlex
from typing import Any

from mentat.command.command import Command
from mentat.errors import SessionExit
from mentat.session_context import SESSION_CONTEXT
from mentat.session_stream import StreamMessage


async def _get_input_request(**kwargs: Any) -> StreamMessage:
    session_context = SESSION_CONTEXT.get()
    stream = session_context.stream

    message = stream.send("", channel="input_request", **kwargs)
    response = await stream.recv(f"input_request:{message.id}")
    logging.debug(f"User Input: {response.data}")
    return response


async def collect_user_input(command_autocomplete: bool = False) -> StreamMessage:
    """
    Listens for user input on a new channel

    send a message requesting user to send a response
    create a new broadcast channel that listens for the input
    close the channel after receiving the input
    """

    response = await _get_input_request(command_autocomplete=command_autocomplete)
    # Quit on q
    if isinstance(response.data, str) and response.data.strip() == "q":
        raise SessionExit

    return response


async def ask_yes_no(default_yes: bool) -> bool:
    session_context = SESSION_CONTEXT.get()
    stream = session_context.stream

    while True:
        # TODO: combine this into a single message (include content)
        stream.send("(Y/n)" if default_yes else "(y/N)")
        response = await collect_user_input()
        content = response.data.strip().lower()
        if content in ["y", "n", ""]:
            break
    return content == "y" or (content != "n" and default_yes)


async def collect_input_with_commands() -> StreamMessage:
    ctx = SESSION_CONTEXT.get()

    response = await collect_user_input(command_autocomplete=True)
    while isinstance(response.data, str) and response.data.startswith("/"):
        try:
            # We only use shlex to split the arguments, not the command itself
            arguments = shlex.split(" ".join(response.data.split(" ")[1:]))
            command = Command.create_command(response.data[1:].split(" ")[0])
            await command.apply(*arguments)
            ctx.code_context.refresh_context_display()
        except ValueError as e:
            ctx.stream.send(f"Error processing command arguments: {e}", style="error")
        response = await collect_user_input(command_autocomplete=True)
    return response
