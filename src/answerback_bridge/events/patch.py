"""RFC 6902 JSON Patch, the subset a state diff needs: add, remove and replace.

`diff` only descends into objects. Arrays are replaced whole: positional array patches are
fragile for a consumer that missed nothing but applies them in a different language.
"""

import copy
from typing import Any

Operation = dict[str, Any]


def escape(token: str) -> str:
    """RFC 6901: `~` first, or an escaped slash would be escaped twice."""
    return token.replace("~", "~0").replace("/", "~1")


def unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def diff(old: Any, new: Any, path: str = "") -> list[Operation]:
    if isinstance(old, dict) and isinstance(new, dict):
        operations: list[Operation] = []
        for key in old:
            if key not in new:
                operations.append({"op": "remove", "path": f"{path}/{escape(key)}"})
        for key, value in new.items():
            pointer = f"{path}/{escape(key)}"
            if key not in old:
                operations.append({"op": "add", "path": pointer, "value": copy.deepcopy(value)})
            else:
                operations.extend(diff(old[key], value, pointer))
        return operations
    if old == new and type(old) is type(new):
        return []
    return [{"op": "replace", "path": path, "value": copy.deepcopy(new)}]


def _tokens(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError("Invalid JSON Pointer")
    return [unescape(token) for token in pointer[1:].split("/")]


def apply(document: Any, operations: list[Operation]) -> Any:
    """Return a patched copy. A path that does not resolve raises, as RFC 6902 requires."""
    result = copy.deepcopy(document)
    for operation in operations:
        kind = operation.get("op")
        tokens = _tokens(operation["path"])
        if kind not in ("add", "remove", "replace"):
            raise ValueError(f"Unsupported patch operation {kind!r}")
        if not tokens:
            if kind == "remove":
                raise ValueError("Cannot remove the document root")
            result = copy.deepcopy(operation["value"])
            continue
        parent = result
        for token in tokens[:-1]:
            parent = _child(parent, token)
        last = tokens[-1]
        if isinstance(parent, dict):
            if kind != "add" and last not in parent:
                raise ValueError("Patch path does not exist")
            if kind == "remove":
                del parent[last]
            else:
                parent[last] = copy.deepcopy(operation["value"])
        elif isinstance(parent, list):
            index = len(parent) if kind == "add" and last == "-" else _index(last)
            if kind == "add" and 0 <= index <= len(parent):
                parent.insert(index, copy.deepcopy(operation["value"]))
            elif kind != "add" and 0 <= index < len(parent):
                if kind == "remove":
                    del parent[index]
                else:
                    parent[index] = copy.deepcopy(operation["value"])
            else:
                raise ValueError("Patch index is out of range")
        else:
            raise ValueError("Patch path does not exist")
    return result


def _index(token: str) -> int:
    if not token.isdigit() or (len(token) > 1 and token[0] == "0"):
        raise ValueError("Invalid array index in patch path")
    return int(token)


def _child(parent: Any, token: str) -> Any:
    if isinstance(parent, dict) and token in parent:
        return parent[token]
    if isinstance(parent, list):
        index = _index(token)
        if index < len(parent):
            return parent[index]
    raise ValueError("Patch path does not exist")
