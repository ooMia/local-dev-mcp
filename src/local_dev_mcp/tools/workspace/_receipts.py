"""Bound mutation retries without retaining request payloads or replaying expired results."""

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from ._mutation_response import FileMutation
from ._reads import Budget
from .errors import WorkspaceError

RECEIPT_COUNT = 128
RECEIPT_SECONDS = 30 * 60
REQUEST_COUNT = 4096


@dataclass
class Receipt:
    request_id: str
    fingerprint: str
    ready: threading.Event = field(default_factory=threading.Event, repr=False)
    result: FileMutation | None = field(default=None, repr=False)
    finished: float | None = None


class MutationReceipts:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._ledger: dict[str, Receipt] = {}
        self._retained: OrderedDict[str, None] = OrderedDict()
        self._closed = False

    def _prune(self) -> None:
        now = time.monotonic()
        for request_id in list(self._retained):
            receipt = self._ledger[request_id]
            if receipt.finished is not None and now - receipt.finished >= RECEIPT_SECONDS:
                self._retained.pop(request_id)
                receipt.result = None
        while len(self._retained) > RECEIPT_COUNT:
            request_id, _ = self._retained.popitem(last=False)
            self._ledger[request_id].result = None

    def claim(self, request_id: str, fingerprint: str, budget: Budget) -> Receipt | FileMutation:
        with self._guard:
            self._prune()
            receipt = self._ledger.get(request_id)
            if receipt is not None:
                if receipt.fingerprint != fingerprint:
                    raise WorkspaceError(
                        "This request_id already names different file mutation arguments. Use a NEW request_id."
                    )
            else:
                if self._closed:
                    raise WorkspaceError("File operations are closing. Reconnect before submitting new work.")
                if len(self._ledger) >= REQUEST_COUNT:
                    raise WorkspaceError(
                        "The file request ledger is full. Inspect current files and restart before new submissions."
                    )
                if sum(not item.ready.is_set() for item in self._ledger.values()) >= RECEIPT_COUNT:
                    raise WorkspaceError("Too many file operations are in progress. Wait before a new submission.")
                receipt = Receipt(request_id, fingerprint)
                self._ledger[request_id] = receipt
                return receipt
        # A duplicate waits for its owner; it never takes the owner's path lock.
        while not receipt.ready.wait(0.1):
            budget.check()
        with self._guard:
            self._prune()
            if receipt.result is None:
                raise WorkspaceError(
                    "This file mutation receipt has expired or was evicted; no operation was replayed. "
                    "Inspect current files before deciding on a new request_id."
                )
            return receipt.result.model_copy(deep=True, update={"replayed": True})

    def finish(self, receipt: Receipt, result: FileMutation) -> None:
        with self._guard:
            receipt.finished = time.monotonic()
            if not self._closed:
                receipt.result = result.model_copy(deep=True)
                self._retained[receipt.request_id] = None
                self._prune()
            receipt.ready.set()

    def clear(self) -> None:
        with self._guard:
            self._closed = True
            for receipt in self._ledger.values():
                receipt.result = None
            self._retained.clear()
