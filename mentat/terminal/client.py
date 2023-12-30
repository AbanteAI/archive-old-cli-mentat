import asyncio
import logging
import signal
from asyncio import Event
from pathlib import Path
from types import FrameType
from typing import Any, Coroutine, List, Optional, Set

import click
from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.styles import Style

import mentat
from mentat.config import update_config
from mentat.session import Session
from mentat.session_stream import StreamMessageSource
from mentat.terminal.loading import LoadingHandler
from mentat.terminal.output import print_stream_message
from mentat.terminal.prompt_completer import MentatCompleter
from mentat.terminal.prompt_session import MentatPromptSession


class TerminalClient:
    def __init__(
        self,
        cwd: Path = Path.cwd(),
        paths: List[str] = [],
        exclude_paths: List[str] = [],
        ignore_paths: List[str] = [],
        diff: str | None = None,
        pr_diff: str | None = None,
    ):
        self.cwd = cwd
        self.paths = [Path(path) for path in paths]
        self.exclude_paths = [Path(path) for path in exclude_paths]
        self.ignore_paths = [Path(path) for path in ignore_paths]
        self.diff = diff
        self.pr_diff = pr_diff

        self._tasks: Set[asyncio.Task[None]] = set()
        self._should_exit = Event()
        self._stopped = Event()

    def _create_task(self, coro: Coroutine[None, None, Any]):
        """Utility method for running a Task in the background"""

        def task_cleanup(task: asyncio.Task[None]):
            self._tasks.remove(task)

        task = asyncio.create_task(coro)
        task.add_done_callback(task_cleanup)
        self._tasks.add(task)

        return task

    async def _cprint_session_stream(self):
        async for message in self.session.stream.listen():
            print_stream_message(message)

    async def _default_prompt_stream(self):
        self._default_prompt = ""
        async for message in self.session.stream.listen("default_prompt"):
            self._default_prompt += message.data

    async def _handle_loading_messages(self):
        loading_handler = LoadingHandler()
        async for message in self.session.stream.listen("loading"):
            loading_handler.update(message)

    async def _handle_input_requests(self):
        while True:
            input_request_message = await self.session.stream.recv("input_request")
            # TODO: Make extra kwargs like plain constants
            if input_request_message.extra.get("plain"):
                prompt_session = self._plain_session
            else:
                prompt_session = self._prompt_session

            default_prompt = self._default_prompt.strip()
            self._default_prompt = ""

            user_input = await prompt_session.prompt_async(
                handle_sigint=False, default=default_prompt
            )
            if user_input == "q":
                self._should_exit.set()
                return

            self.session.stream.send(
                user_input,
                source=StreamMessageSource.CLIENT,
                channel=f"input_request:{input_request_message.id}",
            )

    async def _listen_for_client_exit(self):
        """When the Session shuts down, it will send the client_exit signal for the client to shutdown."""
        await self.session.stream.recv(channel="client_exit")
        asyncio.create_task(self._shutdown())

    async def _listen_for_should_exit(self):
        """This listens for a user event signaling shutdown (like SigInt), and tells the session to shutdown."""
        await self._should_exit.wait()
        self.session.stream.send(
            None, source=StreamMessageSource.CLIENT, channel="session_exit"
        )

    async def _send_session_stream_interrupt(self):
        logging.debug("Sending interrupt to session stream")
        self.session.stream.send(
            "", source=StreamMessageSource.CLIENT, channel="interrupt"
        )

    # Be careful editing this function; since we use signal.signal instead of asyncio's
    # add signal handler (which isn't available on Windows), this function can interrupt
    # asyncio coroutines, potentially causing race conditions.
    def _handle_sig_int(self, sig: int, frame: FrameType | None):
        if (
            # If session is still starting up we want to quit without an error
            not self.session
            or self.session.stream.interrupt_lock.locked() is False
        ):
            if self._should_exit.is_set():
                logging.debug("Force exiting client...")
                exit(0)
            else:
                logging.debug("Should exit client...")
                self._should_exit.set()
        else:
            # We create a task here in order to avoid race conditions
            self._create_task(self._send_session_stream_interrupt())

    def _init_signal_handlers(self):
        signal.signal(signal.SIGINT, self._handle_sig_int)

    async def _run(self):
        self._init_signal_handlers()

        self.session = Session(
            self.cwd,
            self.paths,
            self.exclude_paths,
            self.ignore_paths,
            self.diff,
            self.pr_diff,
        )
        self.session.start()

        config = mentat.user_session.get("config")
        style = Style.from_dict(config.ui.input_style)

        mentat_completer = MentatCompleter(self.session.stream)
        self._prompt_session = MentatPromptSession(
            completer=mentat_completer,
            style=style,
            enable_suspend=True,
        )

        plain_bindings = KeyBindings()

        @plain_bindings.add("c-c")
        @plain_bindings.add("c-d")
        def _(event: KeyPressEvent):
            if event.current_buffer.text != "":
                event.current_buffer.reset()
            else:
                event.app.exit(result="q")

        self._plain_session = PromptSession[str](
            message=[("class:prompt", ">>> ")],
            style=style,
            completer=None,
            key_bindings=plain_bindings,
            enable_suspend=True,
        )

        self._create_task(self._cprint_session_stream())
        self._create_task(self._handle_input_requests())
        self._create_task(self._handle_loading_messages())
        self._create_task(self._default_prompt_stream())
        self._create_task(self._listen_for_client_exit())
        self._create_task(self._listen_for_should_exit())

        logging.debug("Completed startup")
        await self._stopped.wait()

    async def _shutdown(self):
        logging.debug("Running shutdown")

        # Stop all background tasks
        for task in self._tasks:
            task.cancel()
        self._stopped.set()

    def run(self):
        asyncio.run(self._run())


# Event handlers for all the buttons.


@click.command()
@click.option(
    "-e",
    "--exclude-paths",
    multiple=True,
    default=[],
    help="List of file paths, directory paths, or glob patterns to exclude.",
)
@click.option(
    "-g",
    "--ignore-paths",
    multiple=True,
    default=[],
    help=(
        "List of file paths, directory paths, or glob patterns to ignore in"
        " auto-context."
    ),
)
@click.option(
    "-d",
    "--diff",
    default=None,
    show_default="HEAD",
    help="A git tree-ish (e.g. commit, branch, tag) to diff against.",
)
@click.option(
    "-p",
    "--pr-diff",
    default=None,
    help="A git tree-ish to diff against the latest common ancestor of.",
)
@click.option("--cwd", default=str(Path.cwd()), help="The current working directory.")
@click.option("--model", default=None, help="The Model to use.")
@click.option("--temperature", default=None, help="The Model Temperature to use.")
@click.option("--maximum-context", default=None, help="The Maximum Context")
@click.argument("paths", nargs=-1, required=True)
def start(
    paths: list[str],
    exclude_paths: list[str],
    ignore_paths: list[str],
    diff: Optional[str],
    pr_diff: Optional[str],
    cwd: Optional[str],
    model: Optional[str],
    temperature: Optional[float],
    maximum_context: Optional[int],
) -> None:

    if model is not None:
        update_config("model", model)
    if temperature is not None:
        update_config("temperature", temperature)
    if maximum_context is not None:
        update_config("maximum_context", maximum_context)

    current_working_directory = Path.cwd()
    if cwd:
        current_working_directory = Path(cwd).expanduser().resolve()

    terminal_client = TerminalClient(
        current_working_directory, paths, exclude_paths, ignore_paths, diff, pr_diff
    )

    terminal_client.run()


if __name__ == "__main__":
    start()
