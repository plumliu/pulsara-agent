"""Immutable context-input compiler package.

Production callers import the owning submodule directly.  Keeping this package
initializer side-effect free prevents context-input and long-horizon reducers
from acquiring order-dependent import cycles.
"""

__all__: tuple[str, ...] = ()
