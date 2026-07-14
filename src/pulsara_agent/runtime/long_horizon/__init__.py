"""Long-horizon context-window runtime.

Runtime modules import the owning submodule directly. Keeping this package
initializer side-effect free prevents the context-input/session dependency
graph from eagerly importing the subagent execution runtime.
"""

__all__: tuple[str, ...] = ()
