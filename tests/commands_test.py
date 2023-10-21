import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from mentat.commands import Command, ContextCommand, HelpCommand, InvalidCommand
from mentat.session import Session
from mentat.session_context import SESSION_CONTEXT


def test_invalid_command():
    assert isinstance(Command.create_command("non-existent"), InvalidCommand)


@pytest.mark.asyncio
async def test_help_command(mock_session_context):
    command = Command.create_command("help")
    await command.apply()
    assert isinstance(command, HelpCommand)


@pytest.mark.asyncio
async def test_commit_command(
    temp_testbed, mock_setup_api_key, mock_collect_user_input
):
    file_name = "test_file.py"
    with open(file_name, "w") as f:
        f.write("# Commit me!")

    mock_collect_user_input.set_stream_messages(
        [
            "/commit",
            "q",
        ]
    )

    session = Session([])
    await session.start()
    session.stream.stop()

    assert subprocess.check_output(["git", "status", "-s"], text=True) == ""


@pytest.mark.asyncio
async def test_include_command(
    temp_testbed, mock_setup_api_key, mock_collect_user_input
):
    mock_collect_user_input.set_stream_messages(
        [
            "/include scripts",
            "q",
        ]
    )

    session = Session([])
    await session.start()
    session.stream.stop()

    code_context = SESSION_CONTEXT.get().code_context
    assert (
        Path(temp_testbed) / "scripts" / "calculator.py" in code_context.include_files
    )


@pytest.mark.asyncio
async def test_exclude_command(
    temp_testbed, mock_setup_api_key, mock_collect_user_input
):
    mock_collect_user_input.set_stream_messages(
        [
            "/exclude scripts",
            "q",
        ]
    )

    session = Session(["scripts"])
    await session.start()
    session.stream.stop()

    code_context = SESSION_CONTEXT.get().code_context
    assert not code_context.include_files


@pytest.mark.asyncio
async def test_undo_command(
    temp_testbed, mock_setup_api_key, mock_collect_user_input, mock_call_llm_api
):
    temp_file_name = "temp.py"
    with open(temp_file_name, "w") as f:
        f.write(dedent("""\
            # This is a temporary file
            # with 2 lines"""))

    mock_collect_user_input.set_stream_messages(
        [
            "",
            "y",
            "/undo",
            "q",
        ]
    )

    mock_call_llm_api.set_generator_values([dedent(f"""\
        Conversation

        @@start
        {{
            "file": "{temp_file_name}",
            "action": "insert",
            "insert-after-line": 1,
            "insert-before-line": 2
        }}
        @@code
        # I inserted this comment
        @@end""")])

    session = Session([temp_file_name])
    await session.start()
    session.stream.stop()

    with open(temp_file_name, "r") as f:
        content = f.read()
        expected_content = dedent("""\
            # This is a temporary file
            # with 2 lines""")
    assert content == expected_content


@pytest.mark.asyncio
async def test_undo_all_command(
    temp_testbed, mock_setup_api_key, mock_collect_user_input, mock_call_llm_api
):
    temp_file_name = "temp.py"
    with open(temp_file_name, "w") as f:
        f.write(dedent("""\
            # This is a temporary file
            # with 2 lines"""))

    mock_collect_user_input.set_stream_messages(
        [
            "",
            "y",
            "/undo-all",
            "q",
        ]
    )

    # TODO: Make a way to set multiple return values for call_llm_api and reset multiple edits at once
    mock_call_llm_api.set_generator_values([dedent(f"""\
        Conversation

        @@start
        {{
            "file": "{temp_file_name}",
            "action": "insert",
            "insert-after-line": 1,
            "insert-before-line": 2
        }}
        @@code
        # I inserted this comment
        @@end""")])

    session = Session([temp_file_name])
    await session.start()
    session.stream.stop()

    with open(temp_file_name, "r") as f:
        content = f.read()
        expected_content = dedent("""\
            # This is a temporary file
            # with 2 lines""")
    assert content == expected_content


@pytest.mark.asyncio
async def test_clear_command(
    temp_testbed, mock_setup_api_key, mock_collect_user_input, mock_call_llm_api
):
    mock_collect_user_input.set_stream_messages(
        [
            "Request",
            "/clear",
            "q",
        ]
    )
    mock_call_llm_api.set_generator_values(["Answer"])

    session = Session()
    await session.start()
    session.stream.stop()

    conversation = SESSION_CONTEXT.get().conversation
    assert len(conversation.messages) == 1


@pytest.mark.asyncio
async def test_context_command(temp_testbed, mock_setup_api_key, mock_session_context):
    command = Command.create_command("context")
    await command.apply()
    assert isinstance(command, ContextCommand)
