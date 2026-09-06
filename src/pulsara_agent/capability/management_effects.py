"""Physical effects resolved from capability owners, never model risk labels."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResolvedCapabilityEffectProjection:
    workspace_write: bool = False
    outside_workspace_write: bool = False
    process_control: bool = False
    destructive: bool = False

    def __post_init__(self) -> None:
        values = (
            self.workspace_write,
            self.outside_workspace_write,
            self.process_control,
        )
        if any(
            type(value) is not bool for value in (*values, self.destructive)
        ) or not any(values):
            raise ValueError("capability management requires resolved physical effects")
