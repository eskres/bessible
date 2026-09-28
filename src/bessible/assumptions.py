"""Documented assumptions files: every value has a unit, a source and a date."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
ASSUMPTIONS_DIR = DATA_DIR / "assumptions"
FIXTURES_DIR = DATA_DIR / "fixtures"

AssumptionValue = float | list[float] | dict[str, float]


class MissingAssumption(KeyError):  # ruff: ignore[error-suffix-on-exception-name]
    """A computation needs an assumption that is absent or null."""

    def __init__(self, key: str) -> None:
        """Initialize with the missing assumption key."""
        super().__init__(key)
        self.key = key

    def __str__(self) -> str:
        """User-facing missing assumption description."""
        return f"Missing assumption: {self.key}"


class Assumption(BaseModel):
    """One documented value. `placeholder` means the team has not agreed it yet."""

    value: AssumptionValue | None = None
    unit: str = ""
    source: str = ""
    date: str = ""
    status: Literal["agreed", "placeholder"] = "agreed"
    used: bool = True
    source_url: str | None = None
    publisher: str | None = None
    published: str | None = None  # the source's publication date; `date` is when the team checked it
    quote: str | None = None  # verbatim line or table reference from the source
    derivation: str | None = None  # the arithmetic, if any
    note: str | None = None

    @model_validator(mode="after")
    def validate_documented(self) -> Assumption:
        """A value must carry its unit, source and date."""
        if self.value is not None and not (self.unit and self.source and self.date):
            msg = "An assumption with a value needs unit, source and date"
            raise ValueError(msg)
        return self


class AssumptionSet(BaseModel):
    """A loaded assumptions file."""

    entries: dict[str, Assumption] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> AssumptionSet:
        """Read and validate an assumptions file."""
        return cls(entries=json.loads(path.read_text(encoding="utf-8")))

    def entry(self, key: str) -> Assumption:
        """Return the documented entry, or raise `MissingAssumption` naming the key."""
        found = self.entries.get(key)
        if found is None or found.value is None:
            raise MissingAssumption(key)
        return found

    def number(self, key: str) -> float:
        """Return a single-number assumption."""
        value = self.entry(key).value
        if isinstance(value, bool) or not isinstance(value, int | float):
            msg = f"Assumption {key} must be a number"
            raise TypeError(msg)
        return float(value)

    def pair(self, key: str) -> tuple[float, float]:
        """Return a (low, high) assumption."""
        value = self.entry(key).value
        if not isinstance(value, list) or len(value) != 2:  # ruff: ignore[magic-value-comparison]
            msg = f"Assumption {key} must be a [low, high] pair"
            raise TypeError(msg)
        return float(value[0]), float(value[1])

    def mapping(self, key: str) -> dict[str, float]:
        """Return a keyed assumption, for example a table by duration."""
        value = self.entry(key).value
        if not isinstance(value, dict):
            msg = f"Assumption {key} must be a table"
            raise TypeError(msg)
        return value

    def placeholder_keys(self) -> list[str]:
        """Keys that are used by code but not agreed by the team."""
        return sorted(
            k for k, v in self.entries.items() if v.used and v.value is not None and v.status == "placeholder"
        )
