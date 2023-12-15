import hashlib
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

from git import Repo  # type: ignore

from mentat.errors import UserError
from mentat.session_context import SESSION_CONTEXT
from mentat.utils import is_file_text_encoded


def get_non_gitignored_files(path: Path) -> set[Path]:
    return set(
        # git returns / separated paths even on windows, convert so we can remove
        # glob_excluded_files, which have windows paths on windows
        Path(os.path.normpath(p))
        for p in filter(
            lambda p: p != "",
            subprocess.check_output(
                # -c shows cached (regular) files, -o shows other (untracked/new) files
                ["git", "ls-files", "-c", "-o", "--exclude-standard"],
                cwd=path,
                text=True,
                stderr=subprocess.DEVNULL,
            ).split("\n"),
        )
        # windows-safe check if p exists in path
        if Path(path / p).exists()
    )


def get_paths_with_git_diffs(git_root: Path) -> set[Path]:
    changed = subprocess.check_output(
        ["git", "diff", "--name-only"],
        cwd=git_root,
        text=True,
        stderr=subprocess.DEVNULL,
    ).split("\n")
    new = subprocess.check_output(
        ["git", "ls-files", "-o", "--exclude-standard"],
        cwd=git_root,
        text=True,
        stderr=subprocess.DEVNULL,
    ).split("\n")
    return set(
        map(
            lambda path: Path(os.path.realpath(os.path.join(git_root, Path(path)))),
            changed + new,
        )
    )


def get_git_root_for_path(path: Path, raise_error: bool = True) -> Optional[Path]:
    if os.path.isdir(path):
        dir_path = path
    else:
        dir_path = os.path.dirname(path)
    try:
        relative_path = (
            subprocess.check_output(
                [
                    "git",
                    "rev-parse",
                    "--show-prefix",
                ],
                cwd=os.path.realpath(dir_path),
                stderr=subprocess.DEVNULL,
            )
            .decode("utf-8")
            .strip()
        )
        # --show-toplevel doesn't work in some windows environment with posix paths,
        # like msys2, so we have to use --show-prefix instead
        git_root = os.path.abspath(
            os.path.join(dir_path, "../" * len(Path(relative_path).parts))
        )
        # call realpath to resolve symlinks, so all paths match
        return Path(os.path.realpath(git_root))
    except subprocess.CalledProcessError:
        if raise_error:
            logging.error(f"File {path} isn't part of a git project.")
            raise UserError()
        else:
            return


def get_shared_git_root_for_paths(paths: list[Path]) -> Path:
    git_roots = set[Path]()
    for path in paths:
        git_root = get_git_root_for_path(path)
        if git_root is None:
            logging.error(f"File {path} isn't part of a git project.")
            raise UserError()
        git_roots.add(git_root)
    if not paths:
        git_root = get_git_root_for_path(Path(os.getcwd()))
        git_roots.add(git_root)  # pyright: ignore

    if len(git_roots) > 1:
        logging.error(
            "All paths must be part of the same git project! Projects provided:"
            f" {git_roots}"
        )
        raise UserError()
    elif len(git_roots) == 0:
        logging.error("No git projects provided.")
        raise UserError()

    return git_roots.pop()


def commit(message: str) -> None:
    """
    Commit all unstaged and staged changes
    """
    subprocess.run(["git", "add", "."])
    subprocess.run(["git", "commit", "-m", message])


def get_diff_for_file(target: str, path: Path) -> str:
    """Return commit data & diff for target versus active code"""
    session_context = SESSION_CONTEXT.get()

    try:
        diff_content = subprocess.check_output(
            ["git", "diff", "-U0", f"{target}", "--", path],
            cwd=session_context.cwd,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return diff_content
    except subprocess.CalledProcessError:
        logging.error(f"Error obtaining diff for commit '{target}'.")
        raise UserError()


def get_treeish_metadata(git_root: Path, target: str) -> dict[str, str]:
    try:
        commit_info = subprocess.check_output(
            ["git", "log", target, "-n", "1", "--pretty=format:%H %s"],
            cwd=git_root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()

        # Split the returned string into the hash and summary
        commit_hash, commit_summary = commit_info.split(" ", 1)
        return {"hexsha": commit_hash, "summary": commit_summary}
    except subprocess.CalledProcessError:
        logging.error(f"Error obtaining commit data for target '{target}'.")
        raise UserError()


def get_files_in_diff(target: str) -> list[Path]:
    """Return commit data & diff for target versus active code"""
    session_context = SESSION_CONTEXT.get()

    try:
        diff_content = subprocess.check_output(
            ["git", "diff", "--name-only", f"{target}", "--"],
            cwd=session_context.cwd,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if diff_content:
            return [Path(path) for path in diff_content.split("\n")]
        else:
            return []
    except subprocess.CalledProcessError:
        logging.error(f"Error obtaining diff for commit '{target}'.")
        raise UserError()


def check_head_exists() -> bool:
    session_context = SESSION_CONTEXT.get()

    try:
        subprocess.check_output(
            ["git", "rev-parse", "HEAD", "--"],
            cwd=session_context.cwd,
            stderr=subprocess.DEVNULL,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def get_default_branch() -> str:
    session_context = SESSION_CONTEXT.get()

    try:
        # Fetch the symbolic ref of HEAD which points to the default branch
        default_branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=session_context.cwd,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return default_branch
    except subprocess.CalledProcessError:
        # Handle error if needed or raise an exception
        raise Exception("Unable to determine the default branch.")


def get_diff_commit(commit1: str, commit2: str = "HEAD") -> str:
    """Return diff of latest commit against sample_merge_base."""
    session_context = SESSION_CONTEXT.get()
    cwd = session_context.cwd

    repo = Repo(cwd)
    # NOTE: Need at least 1 line of context in order to 'git apply' later
    diff = repo.git.diff(commit1, commit2, unified=1)
    return diff + "\n" if diff else ""  # Required to form a valid .diff file


def get_diff_active(cwd: Path | None = None, target: str = "HEAD") -> str:
    """Return edits to current files + new files in standard git diff format."""
    if cwd is None:
        session_context = SESSION_CONTEXT.get()
        cwd = session_context.cwd

    repo = Repo(cwd)
    diff = repo.git.diff(target, unified=1)
    for new_file in repo.untracked_files:
        if not is_file_text_encoded(cwd / new_file):
            continue
        diff += ("\n" if diff else "") + "\n".join(
            [
                f"diff --git a/{new_file} b/{new_file}",
                "new file mode 100644",
                "index 0000000..ffffff",  # placeholder for git's SHA-1
                "--- /dev/null",
                f"+++ b/{new_file}",
                "@@ -0,0 +1 @@",
            ]
        )
        file_path = cwd / new_file
        with open(file_path, "r") as f:
            file_content = f.read()
        if file_content:
            diff += "\n" + "\n".join(f"+{line}" for line in file_content.splitlines())

    return diff + "\n" if diff else ""  # Required to form a valid .diff file


def get_hexsha_active() -> str:
    """Return the SHA-1 of the current commit."""
    session_context = SESSION_CONTEXT.get()
    cwd = session_context.cwd

    hexsha = ""
    all_files: set[Path] = get_non_gitignored_files(cwd)
    if all_files:
        hasher = hashlib.sha256()
        for file_path in sorted(all_files):
            if file_path.exists() and is_file_text_encoded(file_path):
                hasher.update(file_path.read_bytes())
        hexsha = hasher.hexdigest()
    return hexsha
