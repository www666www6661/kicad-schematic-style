"""Lossless-value S-expression IO and explicit KiCad symbol-library loading.

Formatting and comments are not retained. Quoted strings, bare tokens, numeric
spellings, and unknown list nodes are retained. Derived symbol fields follow
KiCad's flattening rules; display metadata comes from the root symbol and
exclusion flags from the raw immediate parent. Unsupported merges are rejected.
"""

from copy import deepcopy
from pathlib import Path
import re


class Atom(str):
    """A bare S-expression token, including a numeric token."""


_ESCAPES = {'"': '"', "\\": "\\", "a": "\a", "b": "\b", "f": "\f",
            "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
_QUOTED_ESCAPES = {value: "\\" + key for key, value in _ESCAPES.items()}
_MANDATORY_FIELDS = frozenset(("Reference", "Value", "Footprint", "Datasheet", "Description"))
_CANONICAL_FIELDS = {name.casefold(): name for name in _MANDATORY_FIELDS}
_EMPTY_INHERITS = _MANDATORY_FIELDS | {"ki_keywords", "ki_fp_filters"}
_GRAPHIC_NODES = frozenset(("arc", "bezier", "circle", "pin", "polyline", "rectangle", "text", "text_box"))
_ROOT_METADATA = frozenset(("pin_names", "pin_numbers", "power", "body_styles",
                            "duplicate_pin_numbers_are_jumpers", "jumper_pin_groups", "embedded_fonts"))
_EXCLUSION_DEFAULTS = {"exclude_from_sim": "no", "in_bom": "yes", "on_board": "yes", "in_pos_files": "yes"}


def loads(text: str) -> list:
    """Parse exactly one list, retaining the distinction between atoms/strings.

    Unknown string escapes retain their backslash instead of silently losing
    data. No numeric conversion is performed, so e.g. -0.0 remains -0.0.
    """
    if not isinstance(text, str):
        raise TypeError("S-expression input must be text")
    stack = []
    root = None
    index = 0
    while index < len(text):
        token = text[index]
        if token.isspace() or (index == 0 and token == "\ufeff"):
            index += 1
            continue
        if token == "#" and not text[text.rfind("\n", 0, index) + 1:index].lstrip("\ufeff").strip():
            index = text.find("\n", index)
            if index < 0:
                index = len(text)
            continue
        if token == "(":
            node = []
            if stack:
                stack[-1].append(node)
            elif root is not None:
                raise ValueError(f"Multiple root expressions at offset {index}")
            else:
                root = node
            stack.append(node)
            index += 1
            continue
        if token == ")":
            if not stack:
                raise ValueError(f"Unexpected closing parenthesis at offset {index}")
            stack.pop()
            index += 1
            continue
        if not stack:
            raise ValueError(f"Token outside root list at offset {index}")
        if token == '"':
            start = index
            index += 1
            value = bytearray()
            while index < len(text) and text[index] != '"':
                current = text[index]
                if current == "\\":
                    index += 1
                    if index >= len(text):
                        raise ValueError(f"Unterminated string escape at offset {index - 1}")
                    escaped = text[index]
                    if escaped == "x":
                        index += 1
                        digits = ""
                        while index < len(text) and len(digits) < 2 and text[index] in "0123456789abcdefABCDEF":
                            digits += text[index]
                            index += 1
                        value.append(int(digits, 16) if digits else ord("x"))
                        continue
                    if escaped in "01234567":
                        digits = escaped
                        index += 1
                        while index < len(text) and len(digits) < 3 and text[index] in "01234567":
                            digits += text[index]
                            index += 1
                        value.append(int(digits, 8) & 0xff)
                        continue
                    value.extend(_ESCAPES.get(escaped, "\\" + escaped).encode("utf-8"))
                else:
                    value.extend(current.encode("utf-8"))
                index += 1
            if index >= len(text):
                raise ValueError(f"Unterminated quoted string at offset {start}")
            try:
                stack[-1].append(value.decode("utf-8"))
            except UnicodeDecodeError as error:
                raise ValueError(f"Invalid UTF-8 byte escape in string at offset {start}") from error
            index += 1
            if index < len(text) and not (text[index].isspace() or text[index] in "()"):
                raise ValueError(f"Missing token separator at offset {index}")
            continue
        start = index
        while index < len(text) and not (text[index].isspace() or text[index] in '()"'):
            index += 1
        if index < len(text) and text[index] == '"':
            raise ValueError(f"Missing token separator at offset {index}")
        stack[-1].append(Atom(text[start:index]))
    if stack:
        raise ValueError("Unclosed S-expression list")
    if root is None:
        raise ValueError("Expected one root list")
    return root


def _scalar(value):
    if isinstance(value, Atom):
        if not value or any(character.isspace() or character in '()"' for character in value):
            raise ValueError(f"Invalid bare S-expression token: {value!r}")
        return str(value)
    if isinstance(value, str):
        return '"' + "".join(_QUOTED_ESCAPES.get(character,
                            f"\\x{ord(character):02x}" if ord(character) < 32 else character)
                            for character in value) + '"'
    raise TypeError(f"Expected list, str, or Atom; got {type(value).__name__}")


def dumps(tree: list) -> str:
    """Serialize a list with quoted strings and bare Atom values intact."""
    if not isinstance(tree, list):
        raise TypeError("S-expression root must be a list")

    def emit(node, depth):
        if not isinstance(node, list):
            return _scalar(node)
        if not any(isinstance(value, list) for value in node):
            return "(" + " ".join(_scalar(value) for value in node) + ")"
        parts = ["("]
        for index, value in enumerate(node):
            if isinstance(value, list):
                parts.append("\n" + "  " * (depth + 1) + emit(value, depth + 1))
            else:
                parts.append((" " if index else "") + _scalar(value))
        parts.append("\n" + "  " * depth + ")")
        return "".join(parts)

    return emit(tree, 0) + "\n"


def children(node: list, key: str) -> list:
    """Return immediate child lists whose first token matches key."""
    return [item for item in node[1:] if isinstance(item, list) and item and item[0] == key]


def child(node: list, key: str, default=None):
    """Return the first matching immediate child, or default when absent."""
    return next(iter(children(node, key)), default)


class SymbolLibrary:
    """Load symbols only from explicitly mapped .kicad_sym library paths.

    Files are read once per instance. Each load returns an independent tree
    suitable for lib_symbols, with its full library ID and flattened inheritance.
    Inheritance is confined to the named library; no search paths are inferred.
    Omitted mandatory properties inherit the parent's full field, including its
    position and effects, just as explicit empty mandatory properties do.
    """

    def __init__(self, paths: dict[str, str | Path]):
        self.paths = {}
        for name, path in paths.items():
            if not isinstance(name, str) or not name or ":" in name:
                raise ValueError(f"Invalid library nickname: {name!r}")
            self.paths[name] = Path(path)
        self._libraries = {}
        self._resolved = {}

    def _index(self, library):
        if library not in self.paths:
            raise ValueError(f"Unknown library {library!r}; provide an explicit path mapping")
        if library not in self._libraries:
            path = self.paths[library]
            try:
                root = loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as error:
                raise ValueError(f"Cannot read symbol library {library!r} at {path}: {error}") from error
            if not root or root[0] != "kicad_symbol_lib":
                raise ValueError(f"Expected kicad_symbol_lib root in {path}")
            symbols = {}
            for symbol in children(root, "symbol"):
                if len(symbol) < 2 or not isinstance(symbol[1], str) or not symbol[1]:
                    raise ValueError(f"Malformed symbol name in library {library!r}")
                name = str(symbol[1])
                if name in symbols:
                    raise ValueError(f"Duplicate symbol {library}:{name}")
                symbols[name] = symbol
            self._libraries[library] = symbols
        return self._libraries[library]

    @staticmethod
    def _property_indexes(item, identity):
        name_index = 2 if len(item) > 1 and isinstance(item[1], Atom) and item[1] == "private" else 1
        if len(item) <= name_index + 1 or not isinstance(item[name_index], str) or not isinstance(item[name_index + 1], str):
            raise ValueError(f"Malformed property in {identity}")
        return name_index, name_index + 1

    @staticmethod
    def _property_name(item, identity):
        name = item[SymbolLibrary._property_indexes(item, identity)[0]]
        return _CANONICAL_FIELDS.get(name.casefold(), name)

    @staticmethod
    def _validate(symbol, identity):
        properties = set()
        for item in symbol[2:]:
            if not isinstance(item, list) or not item or not isinstance(item[0], str):
                raise ValueError(f"Unsupported top-level item in {identity}: {item!r}")
            if item[0] == "property":
                name = SymbolLibrary._property_name(item, identity)
                if name in properties:
                    raise ValueError(f"Duplicate property {name!r} in {identity}")
                properties.add(name)

    @staticmethod
    def _rename_groups(symbol, old_name, new_name, identity):
        seen = set()
        pattern = re.compile(re.escape(old_name) + r"_(\d+)_(\d+)$")
        for group in children(symbol, "symbol"):
            match = pattern.fullmatch(group[1]) if len(group) >= 2 and isinstance(group[1], str) else None
            if match is None:
                raise ValueError(f"Unsupported nested symbol name in {identity}: {group[1:2]!r}")
            suffix = match.group(1), match.group(2)
            if suffix in seen:
                raise ValueError(f"Duplicate unit/body group in {identity}: {suffix}")
            seen.add(suffix)
            if children(group, "symbol"):
                raise ValueError(f"Unsupported nested symbol inside unit/body group in {identity}")
            group[1] = new_name + "_" + "_".join(suffix)

    @staticmethod
    def _merge(parent, derived, immediate_parent, identity):
        if children(derived, "symbol") or any(item[0] in _GRAPHIC_NODES for item in derived[2:]):
            raise ValueError(f"Unsupported graphic/unit overrides in derived symbol {identity}")
        result = deepcopy(parent)
        # Native Flatten reads these from the raw immediate parent, even in a chain.
        for key, default in _EXCLUSION_DEFAULTS.items():
            inherited = children(immediate_parent, key)
            if len(inherited) > 1 or (inherited and
                    (len(inherited[0]) != 2 or inherited[0][1] not in ("yes", "no"))):
                raise ValueError(f"Unsupported parent exclusion flag {key!r} in {identity}")
            result[2:] = [item for item in result[2:] if item[0] != key]
            result.append(deepcopy(inherited[0]) if inherited else [Atom(key), Atom(default)])
        for item in derived[2:]:
            key = item[0]
            if key == "extends":
                continue
            if key == "property":
                name_index, value_index = SymbolLibrary._property_indexes(item, identity)
                name = SymbolLibrary._property_name(item, identity)
                if name == "ki_locked":
                    continue
                # KiCad inherits these fields/metadata when their child text is empty.
                if name in _EMPTY_INHERITS and item[value_index] == "":
                    continue
                indexes = [index for index, existing in enumerate(result[2:], 2)
                           if existing[0] == "property" and
                           SymbolLibrary._property_name(existing, identity) == name]
            else:
                if key == "embedded_files":
                    raise ValueError(f"Unsupported inherited embedded_files merge in {identity}")
                if len(children(derived, key)) > 1:
                    raise ValueError(f"Unsupported repeated inherited metadata {key!r} in {identity}")
                if key in _ROOT_METADATA or key in _EXCLUSION_DEFAULTS:
                    continue
                raise ValueError(f"Unsupported inherited metadata {key!r} in {identity}")
            if indexes:
                result[indexes[0]] = deepcopy(item)
            else:
                result.append(deepcopy(item))
        return result

    def _resolve(self, library, name, chain):
        key = library, name
        if key in self._resolved:
            return self._resolved[key]
        identity = library + ":" + name
        if name in chain:
            raise ValueError("Symbol inheritance cycle: " + " -> ".join([*chain, name]))
        symbols = self._index(library)
        if name not in symbols:
            trail = " -> ".join([*chain, name])
            raise ValueError(f"Missing symbol {identity} (inheritance chain: {trail})")
        source = symbols[name]
        self._validate(source, identity)
        parents = children(source, "extends")
        if len(parents) > 1:
            raise ValueError(f"Multiple extends declarations in {identity}")
        if parents:
            extends = parents[0]
            if len(extends) != 2 or not isinstance(extends[1], str) or not extends[1] or ":" in extends[1]:
                raise ValueError(f"Unsupported extends target in {identity}; expected a local symbol name")
            parent_name = str(extends[1])
            parent = self._resolve(library, parent_name, (*chain, name))
            result = self._merge(parent, source, symbols[parent_name], identity)
            self._rename_groups(result, parent_name, name, identity)
        else:
            result = deepcopy(source)
            self._rename_groups(result, name, name, identity)
        result[1] = name
        for item in children(result, "property"):
            name_index, _ = self._property_indexes(item, identity)
            item[name_index] = self._property_name(item, identity)
        self._resolved[key] = result
        return result

    def load(self, lib_id: str) -> list:
        """Return a deep-copied, flattened (symbol \"Library:Name\" ...) tree."""
        if not isinstance(lib_id, str) or lib_id.count(":") != 1:
            raise ValueError("Expected a library-qualified symbol ID, e.g. Device:R")
        library, name = lib_id.split(":")
        if not library or not name:
            raise ValueError("Both library nickname and symbol name are required")
        result = deepcopy(self._resolve(library, name, ()))
        result[1] = lib_id
        return result
