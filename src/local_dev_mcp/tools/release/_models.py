"""Explicit compatibility judgements, independent of Git and process execution."""

import re
from collections.abc import Sequence
from typing import Literal, Self, cast

from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

type Impact = Literal["major", "minor", "patch", "none"]

IMPACTS: tuple[Impact, ...] = ("none", "patch", "minor", "major")
IDENTIFIER = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
COMMIT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


def canonical_version(value: str) -> Version:
    if VERSION.fullmatch(value) is None:
        raise ValueError("Use a canonical X.Y.Z version; prerelease, epoch and local forms are not supported.")
    return Version(value)


class Assessment(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    impact: Impact
    summary: str = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=6000)
    contract: str = Field(min_length=1, max_length=1000)
    assessed_by: Literal["user", "agent"]
    migration: str = Field(default="", max_length=6000)
    supersedes: tuple[str, ...] = ()

    @field_validator("summary", "reason", "contract", "migration")
    @classmethod
    def text(cls, value: str) -> str:
        if "\0" in value or (not value.strip() and value != ""):
            raise ValueError("Use explicit nonblank judgement text without NUL.")
        return value

    @field_validator("supersedes", mode="before")
    @classmethod
    def identifiers(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("supersedes must be an array of assessment IDs.")
        items: list[str] = []
        # Pydantic's pre-validation boundary supplies untyped container values.
        for item in cast(Sequence[object], value):
            if not isinstance(item, str):
                raise ValueError("supersedes must be an array of assessment IDs.")
            items.append(item)
        result = tuple(items)
        if any(IDENTIFIER.fullmatch(item) is None for item in result) or len(set(result)) != len(result):
            raise ValueError("Use unique assessment IDs in supersedes.")
        return result

    @model_validator(mode="after")
    def explicit_judgement(self) -> Self:
        if any(not value.strip() for value in (self.summary, self.reason, self.contract)):
            raise ValueError("Summary, reason and contract must contain explicit judgements.")
        if self.impact == "major" and not self.migration.strip():
            raise ValueError("A major assessment requires migration guidance.")
        return self


class WorkflowConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    enabled: Literal[True]
    base_branch: Literal["main"] = "main"
    initial_base: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

    @field_validator("enabled", mode="before")
    @classmethod
    def actual_opt_in(cls, value: object) -> Literal[True]:
        if type(value) is not bool or value is not True:
            raise ValueError("Release opt-in must be the actual boolean true.")
        return True


class ReleaseReceipt(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[1]
    source_head: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    old_version: str
    new_version: str
    consumed: list[str]

    @field_validator("schema_version", mode="before")
    @classmethod
    def actual_schema_version(cls, value: object) -> Literal[1]:
        if type(value) is not int or value != 1:
            raise ValueError("Release schema_version must be the actual integer 1.")
        return 1

    @field_validator("old_version", "new_version")
    @classmethod
    def version(cls, value: str) -> str:
        canonical_version(value)
        return value
