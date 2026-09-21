"""Provider-neutral source shape for visualization review images."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pulsara_agent.tools.builtins.filesystem import IMAGE_REFERENCE_PATTERN


class VisualizationSourceKind(StrEnum):
    PATH = "path"
    VISUALIZATION_REF = "visualization_ref"


@dataclass(frozen=True, slots=True)
class VisualizationSource:
    kind: VisualizationSourceKind
    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("visualization source is empty")
        if self.kind is VisualizationSourceKind.VISUALIZATION_REF and (
            IMAGE_REFERENCE_PATTERN.fullmatch(self.value) is None
        ):
            raise ValueError("visualization reference is invalid")

    def provider_value(self) -> dict[str, str]:
        return {self.kind.value: self.value}


def parse_visualization_source(arguments: dict[str, object]) -> tuple[VisualizationSource, bool]:
    if set(arguments) - {"path", "visualization_ref", "review"}:
        raise ValueError("visualization_render has unsupported arguments")
    path = arguments.get("path")
    reference = arguments.get("visualization_ref")
    review = arguments.get("review", False)
    if not isinstance(review, bool):
        raise ValueError("visualization_render review must be a boolean")
    if (isinstance(path, str) and bool(path)) == (
        isinstance(reference, str) and bool(reference)
    ):
        raise ValueError("visualization_render requires exactly one source")
    if path is not None:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("visualization_render path is invalid")
        return VisualizationSource(VisualizationSourceKind.PATH, path), review
    if not isinstance(reference, str):
        raise ValueError("visualization_render reference is invalid")
    return VisualizationSource(VisualizationSourceKind.VISUALIZATION_REF, reference), review
