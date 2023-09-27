import asyncio
import logging
import signal
from abc import ABC, abstractmethod
from asyncio import Event
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Any, AsyncGenerator

from termcolor import colored

from mentat.code_file_manager import CodeFileManager
from mentat.config_manager import ConfigManager
from mentat.errors import ModelError
from mentat.llm_api import chunk_to_lines
from mentat.parsers.change_display_helper import (
    DisplayInformation,
    FileActionType,
    change_delimiter,
    get_file_name,
    get_later_lines,
    get_previous_lines,
    get_removed_lines,
)
from mentat.parsers.file_edit import FileEdit
from tests.conftest import StreamingPrinter


class Parser(ABC):
    def __init__(self):
        self.shutdown = Event()

    def shutdown_handler(self, sig: int, frame: FrameType | None):
        print("\n\nInterrupted by user. Using the response up to this point.")
        logging.info("User interrupted response.")
        self.shutdown.set()

    # Interface redesign will likely completely change interrupt handling
    @contextmanager
    def interrupt_catcher(self):
        signal.signal(signal.SIGINT, self.shutdown_handler)
        yield
        # Reset to default interrupt handler
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        self.shutdown.clear()

    @abstractmethod
    def get_system_prompt(self) -> str:
        pass

    async def stream_and_parse_llm_response(
        self,
        response: AsyncGenerator[Any, None],
        code_file_manager: CodeFileManager,
        config: ConfigManager,
    ) -> tuple[str, list[FileEdit]]:
        """
        This general parsing structure relies on the assumption that all formats require three types of lines:
        1. 'conversation' lines, which are streamed as they come,
        2. 'special' lines, that are never shown to the user and contain information such as the file_name
        3. 'code' lines, which are the actual code written and are shown to the user in a special format
        To make a parser that differs from these assumptions, make this functionality a subclass of Parser
        """

        printer = StreamingPrinter()
        printer_task = asyncio.create_task(printer.print_lines())
        message = ""
        file_edits = dict[Path, FileEdit]()

        cur_line = ""
        prev_block = ""
        cur_block = ""
        display_information: DisplayInformation | None = None
        file_edit: FileEdit | None = None
        line_printed = False
        in_special_lines = False
        in_code_lines = False
        conversation = True
        printed_delimiter = False
        rename_map = dict[Path, Path]()
        async for chunk in response:
            if self.shutdown.is_set():
                printer_task.cancel()
                break

            for content in chunk_to_lines(chunk):
                if not content:
                    continue
                message += content
                cur_line += content

                # Print if not in special lines and line is confirmed not special
                if not in_special_lines:
                    if not line_printed:
                        if not self._could_be_special(cur_line.strip()):
                            line_printed = True
                            to_print = (
                                cur_line
                                if not in_code_lines or display_information is None
                                else self._code_line_beginning(display_information)
                                + self._code_line_content(cur_line)
                            )
                            printer.add_string(to_print, end="")
                    else:
                        to_print = (
                            content
                            if not in_code_lines
                            else self._code_line_content(content)
                        )
                        printer.add_string(to_print, end="")

                # If we print non code lines, we want to reprint the file name of the next change,
                # even if it's the same file as the last change
                if not in_code_lines and not in_special_lines and line_printed:
                    conversation = True

                # New line handling
                if "\n" in cur_line:
                    if self._starts_special(cur_line.strip()):
                        in_special_lines = True

                    if in_special_lines or in_code_lines:
                        cur_block += cur_line

                    if in_special_lines and self._ends_special(cur_line.strip()):
                        previous_file = (
                            None if file_edit is None else file_edit.file_path
                        )

                        try:
                            display_information, file_edit, in_code_lines = (
                                self._special_block(
                                    code_file_manager, config, rename_map, cur_block
                                )
                            )
                        except ModelError as e:
                            printer.add_string(str(e), color="red")
                            printer.add_string("Using existing changes.")
                            printer.wrap_it_up()
                            await printer_task
                            logging.debug("LLM Response:")
                            logging.debug(message)
                            return (
                                message,
                                [file_edit for file_edit in file_edits.values()],
                            )

                        in_special_lines = False
                        prev_block = cur_block
                        cur_block = ""

                        # Rename map handling
                        if display_information.new_name is not None:
                            rename_map[display_information.new_name] = (
                                display_information.file_name
                            )
                        if display_information.file_name in rename_map:
                            file_edit.file_path = (
                                config.git_root
                                / rename_map[display_information.file_name]
                            )

                        # New file_edit creation and merging
                        if file_edit.file_path not in file_edits:
                            file_edits[file_edit.file_path] = file_edit
                        else:
                            cur_file_edit = file_edits[file_edit.file_path]
                            cur_file_edit.is_creation = (
                                cur_file_edit.is_creation or file_edit.is_creation
                            )
                            cur_file_edit.is_deletion = (
                                cur_file_edit.is_deletion or file_edit.is_deletion
                            )
                            if file_edit.rename_file_path is not None:
                                cur_file_edit.rename_file_path = (
                                    file_edit.rename_file_path
                                )
                            file_edit = cur_file_edit

                        # Print file header
                        if (
                            conversation
                            or display_information.file_action_type
                            == FileActionType.RenameFile
                            or (file_edit.file_path != previous_file)
                        ):
                            conversation = False
                            printer.add_string(get_file_name(display_information))
                            if in_code_lines or display_information.removed_block:
                                printed_delimiter = True
                                printer.add_string(change_delimiter)
                            else:
                                printed_delimiter = False
                        elif not printed_delimiter:
                            # We have to have this so that putting a change like an insert after a rename
                            # still has a change delimiter
                            printer.add_string(change_delimiter)
                            printed_delimiter = True

                        # Print previous lines, removed block, and possibly later lines
                        if in_code_lines or display_information.removed_block:
                            printer.add_string(get_previous_lines(display_information))
                            printer.add_string(get_removed_lines(display_information))
                            if not in_code_lines:
                                printer.add_string(get_later_lines(display_information))
                                printer.add_string(change_delimiter)
                    elif in_code_lines and self._ends_code(cur_line.strip()):
                        # Adding code lines to previous file_edit and printing later lines
                        if display_information is not None and file_edit is not None:
                            self._add_code_block(
                                prev_block, cur_block, display_information, file_edit
                            )
                            printer.add_string(get_later_lines(display_information))
                        printer.add_string(change_delimiter)

                        in_code_lines = False
                        prev_block = cur_block
                        cur_block = ""
                    line_printed = False
                    cur_line = ""
        else:
            # If the model doesn't close out the code lines, we might as well do it for it
            if (
                in_code_lines
                and display_information is not None
                and file_edit is not None
            ):
                self._add_code_block(
                    prev_block, cur_block, display_information, file_edit
                )
                printer.add_string(get_later_lines(display_information))
                printer.add_string(change_delimiter)

            # Only finish printing if we don't quit from ctrl-c
            printer.wrap_it_up()
            await printer_task

        logging.debug("LLM Response:")
        logging.debug(message)
        return (message, [file_edit for file_edit in file_edits.values()])

    # Ideally this would be called in this class instead of subclasses
    def _get_file_lines(
        self,
        code_file_manager: CodeFileManager,
        rename_map: dict[Path, Path],
        rel_path: Path,
    ) -> list[str]:
        path = rename_map.get(
            rel_path,
            rel_path,
        )
        return code_file_manager.file_lines.get(path, [])

    # These 2 methods aren't abstract, since most parsers will use this implementation, but can be overriden easily
    def _code_line_beginning(self, display_information: DisplayInformation) -> str:
        """
        The beginning of a code line; normally this means printing the + prefix
        """
        return colored(
            "+" + " " * (display_information.line_number_buffer - 1), color="green"
        )

    def _code_line_content(self, content: str) -> str:
        """
        Part of a code line; normally this means printing in green
        """
        return colored(content, color="green")

    @abstractmethod
    def _could_be_special(self, cur_line: str) -> bool:
        """
        Returns if this current line could be a special line and therefore shouldn't be printed yet
        """
        pass

    @abstractmethod
    def _starts_special(self, line: str) -> bool:
        """
        Determines if this line begins a special block
        """
        pass

    @abstractmethod
    def _ends_special(self, line: str) -> bool:
        """
        Determines if this line ends a special block
        """
        pass

    @abstractmethod
    def _special_block(
        self,
        code_file_manager: CodeFileManager,
        config: ConfigManager,
        rename_map: dict[Path, Path],
        special_block: str,
    ) -> tuple[DisplayInformation, FileEdit, bool]:
        """
        After finishing special block, return DisplayInformation to print, FileEdit to add/merge to list,
        and if a code block follows this special block.
        """
        pass

    @abstractmethod
    def _ends_code(self, line: str) -> bool:
        """
        Determines if this line ends a code block
        """
        pass

    @abstractmethod
    def _add_code_block(
        self,
        special_block: str,
        code_block: str,
        display_information: DisplayInformation,
        file_edit: FileEdit,
    ):
        """
        Using the special block, code block and display_information, edits the FileEdit to add the new code block
        """
        pass
