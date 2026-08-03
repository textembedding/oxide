import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any


class YamlPayloadError(ValueError):
    pass


_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_BLOCK_HEADER = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*):[ \t]*(?P<style>\|[+-]?)[ \t]*$")


def load_single_string_field(document: str, field: str) -> str:
    if not isinstance(document, str) or not document:
        raise YamlPayloadError("yaml must be a nonempty string")
    if _KEY.fullmatch(field) is None:
        raise YamlPayloadError("invalid YAML field name")
    lines = document.splitlines()
    if not lines:
        raise YamlPayloadError("yaml document must not be empty")
    block = _BLOCK_HEADER.fullmatch(lines[0])
    if block is not None:
        if block.group("key") != field:
            raise YamlPayloadError(f"yaml must contain exactly one field named {field}")
        value = _load_block_scalar(lines[1:])
        if block.group("style") == "|" and value:
            value = value.rstrip("\n") + "\n"
        elif block.group("style") == "|+" and value:
            value += "\n"
        if not value.strip():
            raise YamlPayloadError(f"{field} must be a nonempty string")
        return value
    if len(lines) != 1:
        raise YamlPayloadError("multiline YAML strings require a literal block scalar")
    key, separator, scalar = lines[0].partition(":")
    if not separator or key != field or not scalar.strip():
        raise YamlPayloadError(f"yaml must contain exactly one field named {field}")
    raw = scalar.strip()
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as error:
            raise YamlPayloadError("invalid quoted YAML string") from error
        if not isinstance(value, str):
            raise YamlPayloadError(f"{field} must be a string")
    elif raw.startswith("'"):
        if len(raw) < 2 or not raw.endswith("'"):
            raise YamlPayloadError("invalid quoted YAML string")
        value = raw[1:-1].replace("''", "'")
    else:
        value = raw
    if not value.strip():
        raise YamlPayloadError(f"{field} must be a nonempty string")
    return value


def _load_block_scalar(lines: Sequence[str]) -> str:
    nonempty = [line for line in lines if line.strip()]
    if not nonempty:
        return ""
    indentation = min(len(line) - len(line.lstrip(" ")) for line in nonempty)
    if indentation < 1:
        raise YamlPayloadError("YAML block scalar lines must be indented")
    values: list[str] = []
    for line in lines:
        if line and not line.startswith(" " * indentation):
            raise YamlPayloadError("inconsistent YAML block scalar indentation")
        values.append(line[indentation:] if line else "")
    return "\n".join(values)


def dump_yaml(value: Any) -> str:
    return "\n".join(_yaml_lines(value, 0)) + "\n"


def _yaml_lines(value: Any, indentation: int) -> list[str]:
    prefix = " " * indentation
    if isinstance(value, Mapping):
        if not value:
            return [prefix + "{}"]
        rendered: list[str] = []
        for raw_key, item in value.items():
            key = str(raw_key)
            if _KEY.fullmatch(key) is None:
                raise YamlPayloadError("YAML mapping key is not portable")
            rendered.extend(_mapping_item(key, item, indentation))
        return rendered
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if not value:
            return [prefix + "[]"]
        rendered = []
        for item in value:
            rendered.extend(_sequence_item(item, indentation))
        return rendered
    return [prefix + _inline_scalar(value)]


def _mapping_item(key: str, value: Any, indentation: int) -> list[str]:
    prefix = " " * indentation
    if isinstance(value, str) and "\n" in value:
        style = "|+" if value.endswith("\n") else "|-"
        body = value.split("\n")[:-1] if value.endswith("\n") else value.split("\n")
        return [prefix + key + ": " + style] + [" " * (indentation + 2) + line for line in body]
    if _is_collection(value):
        if not value:
            marker = "{}" if isinstance(value, Mapping) else "[]"
            return [prefix + key + ": " + marker]
        return [prefix + key + ":"] + _yaml_lines(value, indentation + 2)
    return [prefix + key + ": " + _inline_scalar(value)]


def _sequence_item(value: Any, indentation: int) -> list[str]:
    prefix = " " * indentation
    if isinstance(value, str) and "\n" in value:
        style = "|+" if value.endswith("\n") else "|-"
        body = value.split("\n")[:-1] if value.endswith("\n") else value.split("\n")
        return [prefix + "- " + style] + [" " * (indentation + 2) + line for line in body]
    if isinstance(value, Mapping):
        if not value:
            return [prefix + "- {}"]
        items = list(value.items())
        first_key, first_value = items[0]
        first = _mapping_item(str(first_key), first_value, indentation + 2)
        first[0] = prefix + "- " + first[0][indentation + 2 :]
        rendered = first
        for raw_key, item in items[1:]:
            rendered.extend(_mapping_item(str(raw_key), item, indentation + 2))
        return rendered
    if _is_collection(value):
        if not value:
            return [prefix + "- []"]
        return [prefix + "-"] + _yaml_lines(value, indentation + 2)
    return [prefix + "- " + _inline_scalar(value)]


def _is_collection(value: Any) -> bool:
    return isinstance(value, Mapping) or (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
    )


def _inline_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise YamlPayloadError("YAML numbers must be finite")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise YamlPayloadError(f"unsupported YAML value: {type(value).__name__}")
