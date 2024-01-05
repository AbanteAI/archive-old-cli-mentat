import os
from pathlib import Path
from typing import Optional
from uuid import uuid4

from git import GitCommandError, Repo  # type: ignore

from mentat.errors import SampleError
from mentat.git_handler import get_non_gitignored_files
from mentat.utils import is_file_text_encoded

CLONE_TO_DIR = Path(__file__).parent.parent.parent / "benchmark_repos"


def clone_repo(
    url: str, local_dir_name: str, refresh: bool = False, depth: int = 0
) -> Path | None:
    local_dir = CLONE_TO_DIR / local_dir_name
    if os.path.exists(local_dir):
        if refresh:
            repo = Repo(local_dir)
            repo.git.reset("--hard")
            repo.git.clean("-fd")
            repo.git.fetch("--all")
    else:
        if depth > 0:
            repo = Repo.clone_from(url, local_dir, depth=depth)
        else:
            repo = Repo.clone_from(url, local_dir)
    return local_dir


def apply_diff_to_repo(diff: str, repo: Repo, commit: bool = False) -> str | None:
    """Apply a git diff to a repo. If commit is True, commit the changes."""
    temp_id = uuid4().hex
    try:
        # Save self.diff_merge_base to a temporary .diff file
        with open(f".sample_{temp_id}.diff", "w") as f:
            f.write(diff)
        repo.git.execute(["git", "apply", f".sample_{temp_id}.diff"])
        os.remove(f".sample_{temp_id}.diff")
        if commit:
            repo.git.add(".")
            repo.git.commit("-m", f"sample_{temp_id}")
    except GitCommandError as e:
        try:
            os.remove(f".sample_{temp_id}.diff")
        except FileNotFoundError:
            pass
        return str(e)


def get_active_snapshot_commit(repo: Repo) -> str | None:
    """Returns the commit hash of the current active snapshot, or None if there are no active changes."""
    if not repo.is_dirty():
        return None
    if not repo.config_reader().has_option("user", "name"):
        raise SampleError(
            "ERROR: Git user.name not set. Please run 'git config --global user.name"
            ' "Your Name"\'.'
        )
    try:
        # Stash active changes and record the current position
        for file in get_non_gitignored_files(Path(repo.working_dir)):
            if is_file_text_encoded(file):
                repo.git.add(file)
        repo.git.stash("push", "-u")
        detached_head = repo.head.is_detached
        if detached_head:
            current_state = repo.head.commit.hexsha
        else:
            current_state = repo.active_branch.name
        # Commit them on a temporary branch
        temp_branch = f"sample_{uuid4().hex}"
        repo.git.checkout("-b", temp_branch)
        repo.git.stash("apply")
        repo.git.commit("-am", temp_branch)
        # Save the commit hash for diffing against later
        new_commit = repo.head.commit.hexsha
        # Reset repo to how it was before
        repo.git.checkout(current_state)
        repo.git.branch("-D", temp_branch)
        repo.git.stash("apply")
        repo.git.stash("drop")
        # Return the hexsha of the new commit
        return new_commit

    except Exception as e:
        raise SampleError(
            "WARNING: Mentat encountered an error while making temporary git changes:"
            f" {e}. If your active changes have disappeared, they can most likely be "
            "recovered using 'git stash pop'."
        )
