"""Server-local file operations; blocking filesystem work stays off the event loop."""

from typing import Literal

from anyio import from_thread, to_thread

from ...settings import Policy
from . import _discovery
from ._access import Workspace
from ._mutation_response import FileMutation
from ._mutations import FileMutations
from ._read_refs import ReadStore
from ._reads import Budget, read_file
from ._response import FileEdit, FileRead, Listing, PathMatches, TextMatches
from ._writes import WriteLocks, replace_file


class WorkspaceFiles:
    workspace: Workspace
    references: ReadStore
    write_locks: WriteLocks
    mutations: FileMutations

    def __init__(self, policy: Policy) -> None:
        self.workspace = Workspace(policy)
        self.references = ReadStore(self.workspace)
        self.write_locks = WriteLocks()
        self.mutations = FileMutations(self.workspace, self.references, self.write_locks)

    async def list_entries(self, path: str, offset: int, limit: int) -> Listing:
        def work() -> Listing:
            return _discovery.listing(self.workspace.path(path), offset, limit, Budget(from_thread.check_cancelled))

        return await to_thread.run_sync(work)

    async def find_paths(self, pattern: str, path: str, limit: int) -> PathMatches:
        def work() -> PathMatches:
            return _discovery.find_paths(self.workspace.path(path), pattern, limit, Budget(from_thread.check_cancelled))

        return await to_thread.run_sync(work)

    async def search_text(
        self, query: str, path: str, output: Literal["paths", "content"], case_sensitive: bool, limit: int
    ) -> TextMatches:
        def work() -> TextMatches:
            return _discovery.search_text(
                self.workspace.path(path), query, output, case_sensitive, limit, Budget(from_thread.check_cancelled)
            )

        return await to_thread.run_sync(work)

    async def read_file(self, path: str, start_line: int, max_lines: int) -> FileRead:
        # Resolve in the worker as well; policy/path checks can perform filesystem I/O.
        def work() -> FileRead:
            return read_file(self.workspace.path(path), start_line, max_lines, Budget(from_thread.check_cancelled))

        result = await to_thread.run_sync(work)
        if result.content or (result.total_lines == 0 and result.start_line == 1):
            # Publication happens only after the worker's entire observation succeeds.
            result.read_ref = self.references.put(self.workspace.path(result.path), result)
        result.bounded()
        return result

    async def replace_file(self, path: str, read_ref: str, replacement: str) -> FileEdit:
        def work() -> FileEdit:
            return replace_file(
                self.workspace,
                self.references,
                self.write_locks,
                path,
                read_ref,
                replacement,
                Budget(from_thread.check_cancelled),
            )

        return await to_thread.run_sync(work)

    async def create_file(self, path: str, content: str, request_id: str, create_parents: bool = False) -> FileMutation:
        def work() -> FileMutation:
            return self.mutations.create(path, content, request_id, create_parents, Budget(from_thread.check_cancelled))

        return await to_thread.run_sync(work)

    async def delete_file(self, path: str, read_ref: str, request_id: str) -> FileMutation:
        def work() -> FileMutation:
            return self.mutations.delete(path, read_ref, request_id, Budget(from_thread.check_cancelled))

        return await to_thread.run_sync(work)

    def clear(self) -> None:
        self.mutations.clear()
        self.references.clear()
