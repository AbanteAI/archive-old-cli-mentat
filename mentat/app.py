import argparse
import glob
import logging
import os
from typing import Iterable, Optional

from termcolor import cprint

from .code_change import CodeChange
from .code_change_display import print_change
from .code_file_manager import CodeFileManager
from .config_manager import ConfigManager, mentat_dir_path
from .conversation import Conversation
from .errors import MentatError, UserError
from .git_handler import get_shared_git_root_for_paths
from .llm_api import CostTracker, setup_api_key
from .logging_config import setup_logging
from .managers.backup_manager.backup import CodeBackupManager
from .user_input_manager import UserInputManager, UserQuitInterrupt


def run_cli():
    parser = argparse.ArgumentParser(
        description="Run conversation with command line args"
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=[],
        help="List of file paths, directory paths, or glob patterns",
    )
    parser.add_argument(
        "--exclude",
        "-e",
        nargs="*",
        default=[],
        help="List of file paths, directory paths, or glob patterns to exclude",
    )
    parser.add_argument(
        "--revert",
        action="store_true",
        help="Restore from the mentat_backups directory",
    )
    args = parser.parse_args()

    backup_dir = ".mentat_backups"

    if args.revert:
        revert_files(backup_dir)
    else:
        paths = args.paths
        exclude_paths = args.exclude
        run(
            expand_paths(paths),
            expand_paths(exclude_paths),
            backup_dir,
        )


def expand_paths(paths: Iterable[str]) -> Iterable[str]:
    globbed_paths = set()
    invalid_paths = []
    for path in paths:
        new_paths = glob.glob(pathname=path, recursive=True)
        if new_paths:
            globbed_paths.update(new_paths)
        else:
            invalid_paths.append(path)
    if invalid_paths:
        cprint(
            "The following paths do not exist:",
            "light_yellow",
        )
        print("\n".join(invalid_paths))
        exit()
    return globbed_paths


def run(
    paths: Iterable[str],
    exclude_paths: Optional[Iterable[str]] = None,
    backup_dir: Optional[str] = ".mentat_backups",
):
    os.makedirs(mentat_dir_path, exist_ok=True)
    setup_logging()
    logging.debug(f"Paths: {paths}")

    backup_manager = CodeBackupManager(backup_dir)

    cost_tracker = CostTracker()

    try:
        setup_api_key()

        cprint("mentat started with automatic backup pipeline...\n", color="light_blue")

        loop(paths, exclude_paths, cost_tracker, backup_dir)
    except (
        EOFError,
        KeyboardInterrupt,
        UserQuitInterrupt,
        UserError,
        MentatError,
    ) as e:
        if str(e):
            cprint("\n" + str(e), "red")
    finally:
        cost_tracker.display_total_cost()


def loop(
    paths: Iterable[str],
    exclude_paths: Optional[Iterable[str]],
    cost_tracker: CostTracker,
    backup_dir: Optional[str] = ".mentat_backups",
) -> None:
    git_root = get_shared_git_root_for_paths(paths)
    config = ConfigManager(git_root)
    user_input_manager = UserInputManager(config)
    code_file_manager = CodeFileManager(
        paths,
        exclude_paths if exclude_paths is not None else [],
        user_input_manager,
        config,
        git_root,
    )
    conv = Conversation(config, cost_tracker, code_file_manager)

    cprint("Type 'q' or use Ctrl-C to quit at any time.\n", color="cyan")
    cprint("What can I do for you?", color="light_blue")
    need_user_request = True
    while True:
        if need_user_request:
            user_response = user_input_manager.collect_user_input()
            conv.add_user_message(user_response)
        explanation, code_changes = conv.get_model_response(config)

        if code_changes:
            need_user_request = get_user_feedback_on_changes(
                config,
                conv,
                user_input_manager,
                code_file_manager,
                code_changes,
                backup_dir,
            )
        else:
            need_user_request = True


def get_user_feedback_on_changes(
    config: ConfigManager,
    conv: Conversation,
    user_input_manager: UserInputManager,
    code_file_manager: CodeFileManager,
    code_changes: Iterable[CodeChange],
    backup_dir: Optional[str] = ".mentat_backups",
) -> bool:
    cprint(
        "Apply these changes? 'Y/n/i' or provide feedback. mentat will automatically backup your changed files.",
        color="light_blue",
    )
    user_response = user_input_manager.collect_user_input()

    need_user_request = True
    match user_response.lower():
        case "y" | "":
            backup_files(code_file_manager, backup_dir)
            code_changes_to_apply = code_changes
            conv.add_user_message("User chose to apply all your changes.")
        case "n":
            code_changes_to_apply = []
            conv.add_user_message("User chose not to apply any of your changes.")
        case "i":
            code_changes_to_apply, indices = user_filter_changes(
                user_input_manager, code_changes
            )
            conv.add_user_message(
                "User chose to apply"
                f" {len(code_changes_to_apply)}/{len(code_changes)} of your suggest"
                " changes. The changes they applied were:"
                f" {', '.join(map(str, indices))}"
            )
        case _:
            need_user_request = False
            code_changes_to_apply = []
            conv.add_user_message(
                "User chose not to apply any of your changes. User response:"
                f" {user_response}\n\nPlease adjust your previous plan and changes to"
                " reflect this. Respond with a full new set of changes."
            )

    if code_changes_to_apply:
        code_file_manager.write_changes_to_files(code_changes_to_apply)
        if len(code_changes_to_apply) == len(code_changes):
            cprint("Changes applied.", color="light_blue")
        else:
            cprint("Selected changes applied.", color="light_blue")
    else:
        cprint("No changes applied.", color="light_blue")

    if need_user_request:
        cprint("Can I do anything else for you?", color="light_blue")

    return need_user_request


def backup_files(
    code_file_manager: CodeFileManager, backup_dir: Optional[str] = ".mentat_backups"
):
    backup_manager = CodeBackupManager(backup_dir)
    backup_manager.backup_files(code_file_manager)


def user_select_files_to_revert(available_backups: list) -> list:
    cprint(
        "Enter the numbers of the backup files you wish to revert (e.g., '1 3 4') or Press Enter to revert all.",
        color="light_blue",
    )
    user_response = input().strip()

    if not user_response:
        return available_backups

    try:
        selected_indices = list(map(int, user_response.split()))
        selected_files = [
            available_backups[i - 1]
            for i in selected_indices
            if 0 < i <= len(available_backups)
        ]
        return selected_files
    except ValueError:
        cprint("Invalid input. No files reverted.", color="red")
        return []


def revert_files(backup_dir):
    backup_manager = CodeBackupManager(backup_dir)

    available_backups = [
        os.path.relpath(os.path.join(dirpath, filename), backup_dir)
        for dirpath, _, filenames in os.walk(backup_dir)
        for filename in filenames
        if filename.endswith(".backup")
    ]

    available_backups = [filename[:-7] for filename in available_backups]

    if not available_backups:
        cprint("No backup files found for your directory.", color="red")
        return

    cprint("Available backup files are:", color="light_blue")
    for index, backup in enumerate(available_backups, start=1):
        print(f"{index}. {backup.replace('.backup', '')}")

    selected_files = user_select_files_to_revert(available_backups)

    for file in selected_files:
        if backup_manager.restore_file(file):
            cprint(f"Reverted changes for {file} based on backup.", color="green")
        else:
            cprint(f"Failed to revert changes for the file {file}.", color="red")


def user_filter_changes(
    user_input_manager: UserInputManager, code_changes: Iterable[CodeChange]
) -> Iterable[CodeChange]:
    new_changes = []
    indices = []
    for index, change in enumerate(code_changes, start=1):
        print_change(change)
        cprint("Keep this change?", "light_blue")
        if user_input_manager.ask_yes_no(default_yes=True):
            new_changes.append(change)
            indices.append(index)
    return new_changes, indices
