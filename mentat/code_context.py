from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from mentat.code_feature import (
    CodeFeature,
    CodeMessageLevel,
    get_code_message_from_features,
    get_consolidated_feature_refs,
    split_file_into_intervals,
)
from mentat.diff_context import DiffContext
from mentat.feature_filters.default_filter import DefaultFilter
from mentat.feature_filters.embedding_similarity_filter import EmbeddingSimilarityFilter
from mentat.feature_filters.truncate_filter import TruncateFilter
from mentat.git_handler import get_non_gitignored_files, get_paths_with_git_diffs
from mentat.include_files import (
    build_path_tree,
    get_ignore_files,
    get_include_files,
    is_file_text_encoded,
    print_invalid_path,
    print_path_tree,
)
from mentat.llm_api_handler import count_tokens, is_test_environment
from mentat.session_context import SESSION_CONTEXT
from mentat.session_stream import SessionStream
from mentat.utils import sha256


class CodeContext:
    include_files: dict[Path, list[CodeFeature]]
    ignore_files: set[Path]
    diff_context: DiffContext
    features: list[CodeFeature] = []
    diff: Optional[str] = None
    pr_diff: Optional[str] = None

    def __init__(
        self,
        stream: SessionStream,
        git_root: Path,
        diff: Optional[str] = None,
        pr_diff: Optional[str] = None,
    ):
        self.diff = diff
        self.pr_diff = pr_diff
        self.diff_context = DiffContext(stream, git_root, self.diff, self.pr_diff)
        # TODO: This is a dict so we can quickly reference either a path (key)
        # or the CodeFeatures (value) and their intervals. Redundant.
        self.include_files = {}
        self.ignore_files = set()

    def set_paths(
        self,
        paths: list[Path],
        exclude_paths: list[Path],
        ignore_paths: list[Path] = [],
    ):
        if not paths and (self.diff or self.pr_diff) and self.diff_context.files:
            paths = self.diff_context.files
        self.include_files, invalid_paths = get_include_files(paths, exclude_paths)
        for invalid_path in invalid_paths:
            print_invalid_path(invalid_path)
        self.ignore_files = get_ignore_files(ignore_paths)

    def display_context(self):
        """Display the baseline context: included files and auto-context settings"""
        session_context = SESSION_CONTEXT.get()
        stream = session_context.stream
        config = session_context.config
        git_root = session_context.git_root
        prefix = "  "

        stream.send("Code Context:", color="cyan")
        stream.send(f"{prefix}Directory: {git_root}")

        diff = self.diff_context.get_display_context()
        if diff:
            stream.send(f"{prefix}Diff: {diff}", color="green")

        if config.auto_context:
            stream.send(f"{prefix}Auto-Context: Enabled")
            stream.send(f"{prefix}Auto-Tokens: {config.auto_tokens}")
        else:
            stream.send(f"{prefix}Auto-Context: Disabled")

        features = None
        if self.features:
            stream.send(f"{prefix}Active Features:")
            features = self.features
        elif self.include_files:
            stream.send(f"{prefix}Included files:")
            features = [
                _feat for _file in self.include_files.values() for _feat in _file
            ]
        if features is not None:
            refs = get_consolidated_feature_refs(features)
            print_path_tree(
                build_path_tree([Path(r) for r in refs], git_root),
                get_paths_with_git_diffs(),
                git_root,
                prefix + prefix,
            )
        else:
            stream.send(f"{prefix}Included files: None", color="yellow")

    _code_message: str | None = None
    _code_message_checksum: str | None = None

    def _get_code_message_checksum(
        self, prompt: str = "", max_tokens: Optional[int] = None
    ) -> str:
        session_context = SESSION_CONTEXT.get()
        config = session_context.config
        git_root = session_context.git_root
        code_file_manager = session_context.code_file_manager

        if not self.features:
            features_checksum = ""
        else:
            feature_files = {
                git_root / f.path for f in self.features if (git_root / f.path).exists()
            }
            feature_file_checksums = [
                code_file_manager.get_file_checksum(f) for f in feature_files
            ]
            features_checksum = sha256("".join(feature_file_checksums))
        settings = {
            "prompt": prompt,
            "auto_context": config.auto_context,
            "use_llm": self.use_llm,
            "diff": self.diff,
            "pr_diff": self.pr_diff,
            "max_tokens": max_tokens,
            "include_files": self.include_files,
        }
        settings_checksum = sha256(str(settings))
        return features_checksum + settings_checksum

    async def get_code_message(
        self,
        prompt: str,
        max_tokens: int,
        expected_edits: Optional[list[str]] = None,  # for training/benchmarking
        loading_multiplier: float = 0.0,
    ) -> str:
        code_message_checksum = self._get_code_message_checksum(prompt, max_tokens)
        if (
            self._code_message is None
            or code_message_checksum != self._code_message_checksum
        ):
            self._code_message = await self._get_code_message(
                prompt, max_tokens, expected_edits, loading_multiplier
            )
            self._code_message_checksum = self._get_code_message_checksum(
                prompt, max_tokens
            )
        return self._code_message

    use_llm: bool = False

    async def _get_code_message(
        self,
        prompt: str,
        max_tokens: int,
        expected_edits: Optional[list[str]] = None,
        loading_multiplier: float = 0.0,
    ) -> str:
        session_context = SESSION_CONTEXT.get()
        config = session_context.config
        model = config.model

        # Setup code message metadata
        code_message = list[str]()
        self.diff_context.clear_cache()
        if self.diff_context.files:
            code_message += [
                "Diff References:",
                f' "-" = {self.diff_context.name}',
                ' "+" = Active Changes',
                "",
            ]
        code_message += ["Code Files:\n"]
        meta_tokens = count_tokens("\n".join(code_message), model, full_message=True)
        remaining_tokens = max_tokens - meta_tokens
        auto_tokens = min(remaining_tokens, config.auto_tokens)

        if remaining_tokens < 0:
            self.features = []
            return ""
        elif not config.auto_context:
            self.features = self._get_include_features()
            if sum(f.count_tokens(model) for f in self.features) > remaining_tokens:
                if prompt and not is_test_environment():
                    self.features = await EmbeddingSimilarityFilter(prompt).filter(
                        self.features
                    )
                self.features = await TruncateFilter(
                    remaining_tokens, model, respect_user_include=False
                ).filter(self.features)
        else:
            self.features = self._get_all_features(
                CodeMessageLevel.INTERVAL,
            )
            feature_filter = DefaultFilter(
                auto_tokens,
                self.use_llm,
                prompt,
                expected_edits,
                loading_multiplier=loading_multiplier,
            )
            self.features = await feature_filter.filter(self.features)

        # Group intervals by file, separated by ellipses if there are gaps
        code_message += get_code_message_from_features(self.features)
        return "\n".join(code_message)

    def _get_include_features(self) -> list[CodeFeature]:
        session_context = SESSION_CONTEXT.get()
        git_root = session_context.git_root

        include_features = list[CodeFeature]()
        for path, features in self.include_files.items():
            annotations = self.diff_context.get_annotations(path)
            for feature in features:
                has_diff = any(a.intersects(feature.interval) for a in annotations)
                feature = CodeFeature(
                    feature.ref(),
                    feature.level,
                    diff=self.diff_context.target if has_diff else None,
                    user_included=True,
                )
                include_features.append(feature)

        def _feature_relative_path(f: CodeFeature) -> str:
            return os.path.relpath(f.path, git_root)

        return sorted(include_features, key=_feature_relative_path)

    def _get_all_features(
        self,
        level: CodeMessageLevel,
        max_chars: int = 100000,
    ) -> list[CodeFeature]:
        session_context = SESSION_CONTEXT.get()
        git_root = session_context.git_root

        all_features = list[CodeFeature]()
        for path in get_non_gitignored_files(git_root):
            abs_path = git_root / path
            if (
                abs_path.is_dir()
                or not is_file_text_encoded(abs_path)
                or abs_path in self.ignore_files
                or os.path.getsize(abs_path) > max_chars
            ):
                continue

            diff_target = (
                self.diff_context.target
                if abs_path in self.diff_context.files
                else None
            )
            user_included = abs_path in self.include_files
            if level == CodeMessageLevel.INTERVAL:
                full_feature = CodeFeature(
                    abs_path,
                    level=CodeMessageLevel.CODE,
                    diff=diff_target,
                    user_included=user_included,
                )
                _split_features = split_file_into_intervals(
                    git_root,
                    full_feature,
                    user_features=self.include_files.get(abs_path, []),
                )
                all_features += _split_features
            else:
                _feature = CodeFeature(
                    abs_path, level=level, diff=diff_target, user_included=user_included
                )
                all_features.append(_feature)

        return sorted(all_features, key=lambda f: f.path)

    def include_file(self, path: Path):
        paths, invalid_paths = get_include_files([path], [])
        for new_path, new_features in paths.items():
            if new_path not in self.include_files:
                self.include_files[new_path] = []
            for feature in new_features:
                self.include_files[new_path].append(feature)
        return list(paths.keys()), invalid_paths

    def exclude_file(self, path: Path):
        # TODO: Using get_include_files here isn't ideal; if the user puts in a glob that
        # matches files but doesn't match any files in context, we won't know what that glob is
        # and can't return it as an invalid path
        paths, invalid_paths = get_include_files([path], [])
        removed_paths = list[Path]()
        for new_path in paths.keys():
            if new_path in self.include_files:
                removed_paths.append(new_path)
                del self.include_files[new_path]
        return removed_paths, invalid_paths

    async def search(
        self,
        query: str,
        max_results: int | None = None,
        level: CodeMessageLevel = CodeMessageLevel.INTERVAL,
    ) -> list[tuple[CodeFeature, float]]:
        """Return the top n features that are most similar to the query."""

        all_features = self._get_all_features(
            level,
        )

        embedding_similarity_filter = EmbeddingSimilarityFilter(query)
        all_features_sorted = await embedding_similarity_filter.score(all_features)
        if max_results is None:
            return all_features_sorted
        else:
            return all_features_sorted[:max_results]
