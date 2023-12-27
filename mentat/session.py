import asyncio
import logging
import os
import traceback
from asyncio import CancelledError, Task
from pathlib import Path
from typing import Any, Coroutine, List, Optional, Set
from uuid import uuid4
from rich import print
from rich.console import Console

import attr
import sentry_sdk
from openai import APITimeoutError, BadRequestError, RateLimitError

from mentat.agent_handler import AgentHandler
from mentat.auto_completer import AutoCompleter
from mentat.code_context import CodeContext
from mentat.code_edit_feedback import get_user_feedback_on_edits
from mentat.code_file_manager import CodeFileManager
from mentat.config import config
from mentat.conversation import Conversation
from mentat.cost_tracker import CostTracker
from mentat.ctags import ensure_ctags_installed
from mentat.errors import MentatError, SessionExit, UserError, ContextSizeInsufficient
from mentat.git_handler import get_git_root_for_path
from mentat.llm_api_handler import LlmApiHandler, is_test_environment
from mentat.logging_config import setup_logging
from mentat.sentry import sentry_init
from mentat.session_context import SESSION_CONTEXT, SessionContext
from mentat.session_input import collect_input_with_commands
from mentat.session_stream import SessionStream
from mentat.utils import check_version, mentat_dir_path
from mentat.vision.vision_manager import VisionManager
from mentat.sampler.sampler import Sampler

console = Console()

class Session:
    """
    The server for Mentat.
    To stop, send a message on the session_exit channel.
    A message will be sent on the client_exit channel when ready for client to quit.
    """

    def __init__(
        self,
        cwd: Path,
        paths: List[Path] = [],
        exclude_paths: List[Path] = [],
        ignore_paths: List[Path] = [],
        diff: Optional[str] = None,
        pr_diff: Optional[str] = None,
    ):
        # All errors thrown here need to be caught here
        self._errors = []
        self.stopped = False

        if not mentat_dir_path.exists():
            os.mkdir(mentat_dir_path)
        setup_logging()
        sentry_init()
        self.id = uuid4()
        self._tasks: Set[asyncio.Task[None]] = set()

        # Since we can't set the session_context until after all of the singletons are created,
        # any singletons used in the constructor of another singleton must be passed in
        git_root = get_git_root_for_path(cwd, raise_error=False)

        llm_api_handler = LlmApiHandler()

        stream = SessionStream()
        self.stream = stream
        self.stream.start()

        cost_tracker = CostTracker()

        code_context = CodeContext(stream, git_root, diff, pr_diff, ignore_paths)

        code_file_manager = CodeFileManager()

        conversation = Conversation()

        vision_manager = VisionManager()

        agent_handler = AgentHandler()

        auto_completer = AutoCompleter()

        sampler = Sampler()

        session_context = SessionContext(
            cwd=cwd,
            stream=stream,
            llm_api_handler=llm_api_handler,
            cost_tracker=cost_tracker,
            code_context=code_context,
            code_file_manager=code_file_manager,
            conversation=conversation,
            vision_manager=vision_manager,
            agent_handler=agent_handler,
            auto_completer=auto_completer,
            sampler=sampler
        )
        self.ctx = session_context
        SESSION_CONTEXT.set(session_context)

        # Functions that require session_context
        check_version()
        self.send_errors_to_stream()
        for path in paths:
            code_context.include(path, exclude_patterns=exclude_paths)

    def _create_task(self, coro: Coroutine[None, None, Any]):
        """Utility method for running a Task in the background"""

        def task_cleanup(task: asyncio.Task[None]):
            self._tasks.remove(task)

        task = asyncio.create_task(coro)
        task.add_done_callback(task_cleanup)
        self._tasks.add(task)

        return task

    async def _main(self):
        session_context = SESSION_CONTEXT.get()
        stream = session_context.stream
        code_context = session_context.code_context
        conversation = session_context.conversation
        code_file_manager = session_context.code_file_manager
        agent_handler = session_context.agent_handler

        # check early for ctags so we can fail fast
        if config.run.auto_context_tokens > 0:
            ensure_ctags_installed()

        session_context.llm_api_handler.initialize_client()
        code_context.display_context()
        await conversation.display_token_count()

        print(f"Type 'q' or use Ctrl-C to quit at any time.")
        need_user_request = True
        while True:
            try:
                if need_user_request:
                    # Normally, the code_file_manager pushes the edits; but when agent mode is on, we want all
                    # edits made between user input to be collected together.
                    if agent_handler.agent_enabled:
                        code_file_manager.history.push_edits()
                        print(f"[green]Use /undo to undo all changes from agent mode since last input.[/green]")

                    print(f"[blue]What can I do for you?[/blue]")
                    message = await collect_input_with_commands()
                    if message.data.strip() == "":
                        continue
                    conversation.add_user_message(message.data)

                parsed_llm_response = await conversation.get_model_response()
                file_edits = [
                    file_edit
                    for file_edit in parsed_llm_response.file_edits
                    if file_edit.is_valid()
                ]
                if file_edits:
                    if not agent_handler.agent_enabled:
                        file_edits, need_user_request = (
                            await get_user_feedback_on_edits(file_edits)
                        )
                    for file_edit in file_edits:
                        file_edit.resolve_conflicts()

                    if session_context.sampler:
                        session_context.sampler.set_active_diff()

                    applied_edits = await code_file_manager.write_changes_to_files(
                        file_edits
                    )

                    if applied_edits:
                        print(f"[blue]Changes applied.[/blue]")
                    else:
                        print(f"[blue]No Changes applied.[/blue]")

                    if agent_handler.agent_enabled:
                        if parsed_llm_response.interrupted:
                            need_user_request = True
                        else:
                            need_user_request = await agent_handler.add_agent_context()
                else:
                    need_user_request = True
                stream.send(bool(file_edits), channel="edits_complete")
            except SessionExit:
                break
            except ContextSizeInsufficient:
                need_user_request = True
                continue
            except (APITimeoutError, RateLimitError, BadRequestError) as e:
                print(f"[red]Error accessing OpenAI API: {e.message}[/red]")
                break

    async def listen_for_session_exit(self):
        await self.stream.recv(channel="session_exit")
        self._main_task.cancel()

    async def listen_for_completion_requests(self):
        ctx = SESSION_CONTEXT.get()

        async for message in self.stream.listen(channel="completion_request"):
            completions = ctx.auto_completer.get_completions(message.data)
            # Will intermediary client for vscode serialize/deserialize all messages automatically?
            self.stream.send(completions, channel=f"completion_request:{message.id}")

    ### lifecycle

    def start(self):
        """Asynchronously start the Session.

        A background asyncio Task will be created to run the startup sequence and run
        the main loop which runs until an Exception or session_exit signal is encountered.
        """

        async def run_main():
            ctx = SESSION_CONTEXT.get()
            try:
                with sentry_sdk.start_transaction(
                    op="mentat_started", name="Mentat Started"
                ) as transaction:
                    #TODO: check if we need this as config should be gloabl now
                    #transaction.set_tag("config", attr.asdict(config))
                    await self._main()
            except (SessionExit, CancelledError):
                pass
            except (MentatError, UserError) as e:
                if is_test_environment():
                    console.print_exception(show_locals=True)
                print(f"[red]Unhandled Exception: {str(e)}[/red]")
            except Exception as e:
                # All unhandled exceptions end up here
                error = f"Unhandled Exception: {traceback.format_exc()}"
                # Helps us handle errors in tests
                if is_test_environment():
                    console.print_exception(show_locals=True)
                self.error = error
                sentry_sdk.capture_exception(e)
                print(f"[red]{str(error)}[/red]")
            finally:
                await self._stop()
                sentry_sdk.flush()

        self._main_task: Task[None] = asyncio.create_task(run_main())

        self._create_task(self.listen_for_session_exit())
        self._create_task(self.listen_for_completion_requests())

    async def _stop(self):
        if self.stopped:
            return
        self.stopped = True

        session_context = SESSION_CONTEXT.get()
        cost_tracker = session_context.cost_tracker
        vision_manager = session_context.vision_manager

        vision_manager.close()
        cost_tracker.display_total_cost()
        logging.shutdown()

        for task in self._tasks:
            task.cancel()

        self._main_task.cancel()
        try:
            await self._main_task
        except CancelledError:
            pass

        self.stream.send(None, channel="client_exit")
        await self.stream.join()
        self.stream.stop()

    def send_errors_to_stream(self):
        session_context = SESSION_CONTEXT.get()
        stream = session_context.stream
        for error in self._errors:
            print(f"[light_yellow3]{error}[/light_yellow3]")
        self._errors = []
