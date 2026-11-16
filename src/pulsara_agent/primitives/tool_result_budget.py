"""Failure at an explicitly bounded per-operation text selection boundary."""

class ToolResultPageBoundary(ValueError):
    """The smallest complete selection unit cannot fit the operation allowance."""
