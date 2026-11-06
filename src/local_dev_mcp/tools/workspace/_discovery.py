"""Bounded top-down discovery; ignore patterns never override access checks."""

import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from pathspec import GitIgnoreSpec

from ._access import ScopedPath, protected
from ._reads import Budget, ScanStopped, SkippedFile, TextObservation
from ._response import Entry, FileResult, Listing, Match, PathMatches, TextMatches
from .errors import WorkspaceError


@dataclass(frozen=True)
class IgnoreRules:
    layers: tuple[tuple[ScopedPath, GitIgnoreSpec], ...] = ()

    def excluded(self, path: ScopedPath, directory: bool) -> bool:
        excluded = False
        for base, spec in self.layers:
            relative = PurePosixPath(path.relative).relative_to(PurePosixPath(base.relative)).as_posix()
            result = spec.check_file(relative + ("/" if directory else ""))
            if result.include is not None:
                excluded = result.include
        return excluded

    def enter(self, directory: ScopedPath, budget: Budget) -> IgnoreRules:
        raw = directory.checked() / ".gitignore"
        try:
            raw.lstat()
        except FileNotFoundError:
            return self
        except OSError:
            raise WorkspaceError.ignore() from None
        try:
            path = directory.join(".gitignore")
            # Policy input is consumed internally even when only a subtree is visible.
            spec = GitIgnoreSpec.from_lines(TextObservation(path, budget, policy_metadata=True).lines())
        except WorkspaceError, SkippedFile, ValueError:
            raise WorkspaceError.ignore() from None
        return IgnoreRules((*self.layers, (directory, spec)))


def initial_rules(start: ScopedPath, budget: Budget) -> IgnoreRules | None:
    current = start.workspace.path(".")
    rules = IgnoreRules().enter(current, budget)
    for part in PurePosixPath(start.relative).parts:
        current = current.join(part)
        if rules.excluded(current, True):
            return None
        rules = rules.enter(current, budget)
    return rules


def record_io(result: FileResult) -> None:
    result.stop("read_errors")
    if isinstance(result, TextMatches):
        result.skipped.io += 1


def children(directory: ScopedPath, budget: Budget, result: FileResult, *, required: bool = False) -> list[Entry]:
    items: list[Entry] = []
    try:
        with os.scandir(directory.checked()) as stream:
            for entry in stream:
                budget.entry()
                if protected(entry.name):
                    continue
                try:
                    entry.name.encode("utf-8")
                    relative = str(PurePosixPath(directory.relative) / entry.name)
                    if not directory.workspace.policy.allows_path(
                        directory.workspace.policy.root / relative, traverse=True
                    ):
                        continue
                    path = ScopedPath(directory.workspace, relative)
                    info = path.checked(final_link=True).lstat()
                    kind: Literal["file", "directory", "symlink", "other"]
                    if stat.S_ISLNK(info.st_mode):
                        kind = "symlink"
                    elif stat.S_ISDIR(info.st_mode):
                        kind = "directory"
                    elif stat.S_ISREG(info.st_mode):
                        kind = "file"
                    else:
                        kind = "other"
                    items.append(Entry(path=relative, kind=kind))
                except UnicodeError, OSError, WorkspaceError:
                    record_io(result)
    except ScanStopped as error:
        result.stop(error.reason)
    except OSError:
        if required:
            raise WorkspaceError.access() from None
        record_io(result)
    return sorted(items, key=lambda item: item.path)


def listing(start: ScopedPath, offset: int, limit: int, budget: Budget) -> Listing:
    if not start.checked().is_dir():
        raise WorkspaceError.access()
    result = Listing(project=str(start.workspace.policy.root), path=start.relative)
    try:
        rules = initial_rules(start, budget)
        if rules is None:
            return result
        visible = 0
        for item in children(start, budget, result, required=True):
            path = ScopedPath(start.workspace, item.path)
            if not start.workspace.policy.allows_path(
                start.workspace.policy.root / item.path, traverse=item.kind == "directory"
            ):
                continue
            if rules.excluded(path, item.kind == "directory"):
                continue
            if visible < offset:
                visible += 1
                continue
            if len(result.entries) >= limit:
                result.stop("result_limit")
                result.next_offset = offset + len(result.entries)
                break
            result.entries.append(item)
            if not result.fits():
                result.entries.pop()
                result.stop("response_limit")
                result.next_offset = offset + len(result.entries)
                break
            visible += 1
    except ScanStopped as error:
        result.stop(error.reason)
    result.bounded()
    return result


def walk(start: ScopedPath, budget: Budget, result: FileResult) -> Iterator[ScopedPath]:
    if not start.checked().is_dir():
        raise WorkspaceError.access()
    rules = initial_rules(start, budget)
    if rules is None:
        return
    stack: list[tuple[ScopedPath, IgnoreRules, bool]] = [(start, rules, True)]
    while stack:
        directory, rules, entered = stack.pop()
        budget.check()
        if not entered:
            rules = rules.enter(directory, budget)
        items = children(directory, budget, result, required=directory == start)
        exhausted = result.reason in ("scan_limit", "deadline")
        subdirectories: list[tuple[ScopedPath, IgnoreRules, bool]] = []
        for item in items:
            budget.check()
            path = ScopedPath(start.workspace, item.path)
            if not start.workspace.policy.allows_path(
                start.workspace.policy.root / item.path, traverse=item.kind == "directory"
            ):
                continue
            if rules.excluded(path, item.kind == "directory"):
                continue
            if item.kind == "directory":
                subdirectories.append((path, rules, False))
            elif item.kind == "file":
                yield path
            elif item.kind == "other" and isinstance(result, TextMatches):
                result.skipped.special += 1
        if exhausted:
            return
        stack.extend(reversed(subdirectories))


def find_paths(start: ScopedPath, pattern: str, limit: int, budget: Budget) -> PathMatches:
    if not pattern or "\0" in pattern or PurePosixPath(pattern).is_absolute() or ".." in PurePosixPath(pattern).parts:
        raise WorkspaceError.arguments("pattern", "Use a nonempty relative glob without workspace escapes.")
    result = PathMatches(project=str(start.workspace.policy.root), path=start.relative, pattern=pattern)
    try:
        for path in walk(start, budget, result):
            relative = PurePosixPath(path.relative).relative_to(PurePosixPath(start.relative))
            if not relative.full_match(pattern, case_sensitive=True):
                continue
            if len(result.paths) >= limit:
                result.stop("result_limit")
                break
            result.paths.append(path.relative)
            if not result.fits():
                result.paths.pop()
                result.stop("response_limit")
                break
    except ScanStopped as error:
        result.stop(error.reason)
    result.bounded()
    return result


def search_text(
    start: ScopedPath, query: str, output: Literal["paths", "content"], case_sensitive: bool, limit: int, budget: Budget
) -> TextMatches:
    if not query or any(char in query for char in "\0\r\n"):
        raise WorkspaceError.arguments("query", "Use a nonempty single-line literal string without NUL.")
    if output == "content" and limit > 100:
        raise WorkspaceError.arguments("limit", "Content output permits at most 100 matching lines.")
    result = TextMatches(project=str(start.workspace.policy.root), path=start.relative, output=output)
    needle = query if case_sensitive else query.casefold()
    try:
        for path in walk(start, budget, result):
            candidates: list[Match] = []
            matched = False
            try:
                # Finish validation before publishing any matches from this file.
                for number, line in enumerate(TextObservation(path, budget).lines(), 1):
                    if needle not in (line if case_sensitive else line.casefold()):
                        continue
                    matched = True
                    if output == "content" and len(candidates) <= limit:
                        encoded = line.removesuffix("\n").removesuffix("\r").encode("utf-8")
                        preview = encoded[:512].decode("utf-8", errors="ignore")
                        candidates.append(
                            Match(
                                path=path.relative, line=number, preview=preview, preview_truncated=len(encoded) > 512
                            )
                        )
            except SkippedFile as error:
                setattr(result.skipped, error.kind, getattr(result.skipped, error.kind) + 1)
                continue
            except WorkspaceError:
                record_io(result)
                continue
            if not matched:
                continue
            if output == "paths":
                if len(result.paths) >= limit:
                    result.stop("result_limit")
                    break
                result.paths.append(path.relative)
                if not result.fits():
                    result.paths.pop()
                    result.stop("response_limit")
                    break
            else:
                for match in candidates:
                    if len(result.matches) >= limit:
                        result.stop("result_limit")
                        break
                    result.matches.append(match)
                    if not result.fits():
                        result.matches.pop()
                        result.stop("response_limit")
                        break
                if result.reason in ("result_limit", "response_limit"):
                    break
    except ScanStopped as error:
        result.stop(error.reason)
    result.bounded()
    return result
