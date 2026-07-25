"""Durable projection job ownership and canonical mutation delivery.

The package root intentionally performs no eager imports. Low-level storage
authority imports the pure contracts module and must not initialize EventLog
or executable registries as a side effect.
"""

__all__: list[str] = []
