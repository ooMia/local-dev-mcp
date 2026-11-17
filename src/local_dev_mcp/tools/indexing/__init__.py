"""Optional Trail index management for the one configured project root."""

from ._manager import IndexManager
from .get_project_index_status import GET_PROJECT_INDEX_STATUS
from .refresh_project_index import REFRESH_PROJECT_INDEX
from .worktree_index import GET_PROJECT_WORKTREE_INDEX_STATUS, REFRESH_PROJECT_WORKTREE_INDEX

__all__ = [
    "GET_PROJECT_INDEX_STATUS",
    "GET_PROJECT_WORKTREE_INDEX_STATUS",
    "REFRESH_PROJECT_INDEX",
    "REFRESH_PROJECT_WORKTREE_INDEX",
    "IndexManager",
]
