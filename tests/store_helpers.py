"""Register the loaded plugins' store hooks on a store built outside adapter composition."""

from answerback_bridge.adapters.builtins import builtin_plugins
from answerback_bridge.tasks import TaskStore


def register_builtin_store_hooks(store: TaskStore) -> TaskStore:
    """Register every loaded plugin's hooks as `build_adapters` does for direct fixtures."""
    for plugin in builtin_plugins():
        hooks = plugin.store_hooks()
        if hooks is not None:
            store.hooks.register(plugin.definition.name, hooks)
    return store
