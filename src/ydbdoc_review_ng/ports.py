"""Narrow structural ports for domain and application code."""

from datetime import datetime
from typing import Protocol, TypeVar

from ydbdoc_review_ng.domain import RepoPath, SnapshotRef

__all__ = [
    "Clock",
    "GitHubGateway",
    "ModelClient",
    "RequestT_contra",
    "ResultT_co",
    "SnapshotReader",
    "StateStore",
]

RequestT_contra = TypeVar("RequestT_contra", contravariant=True)
ResultT_co = TypeVar("ResultT_co", covariant=True)


class SnapshotReader(Protocol):
    def read_bytes(self, snapshot: SnapshotRef, path: RepoPath, /) -> bytes | None: ...


class ModelClient(Protocol[RequestT_contra, ResultT_co]):
    def invoke(self, request: RequestT_contra, /) -> ResultT_co: ...


class StateStore(Protocol[RequestT_contra, ResultT_co]):
    def execute(self, request: RequestT_contra, /) -> ResultT_co: ...


class GitHubGateway(Protocol[RequestT_contra, ResultT_co]):
    def execute(self, request: RequestT_contra, /) -> ResultT_co: ...


class Clock(Protocol):
    def now(self, /) -> datetime: ...
