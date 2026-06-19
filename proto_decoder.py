"""Small protobuf wire-format decoder for unknown Antigravity step payloads."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProtoField:
    number: int
    wire_type: int
    value: Any
    raw: bytes | None = None


def decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            raise ValueError("varint is too long")
    raise ValueError("truncated varint")


def decode_message(data: bytes, *, max_depth: int = 8) -> list[ProtoField]:
    fields, offset = _decode_message(data, depth=0, max_depth=max_depth)
    if offset != len(data):
        return fields
    return fields


def _decode_message(data: bytes, *, depth: int, max_depth: int) -> tuple[list[ProtoField], int]:
    fields: list[ProtoField] = []
    offset = 0

    while offset < len(data):
        try:
            tag, offset = decode_varint(data, offset)
        except ValueError:
            break

        number = tag >> 3
        wire_type = tag & 7
        if number <= 0:
            break

        try:
            if wire_type == 0:
                value, offset = decode_varint(data, offset)
                fields.append(ProtoField(number, wire_type, value))
            elif wire_type == 1:
                if offset + 8 > len(data):
                    break
                raw = data[offset : offset + 8]
                fields.append(ProtoField(number, wire_type, struct.unpack("<Q", raw)[0], raw))
                offset += 8
            elif wire_type == 2:
                length, offset = decode_varint(data, offset)
                if offset + length > len(data):
                    break
                raw = data[offset : offset + length]
                offset += length
                fields.append(ProtoField(number, wire_type, _decode_len(raw, depth, max_depth), raw))
            elif wire_type == 5:
                if offset + 4 > len(data):
                    break
                raw = data[offset : offset + 4]
                fields.append(ProtoField(number, wire_type, struct.unpack("<I", raw)[0], raw))
                offset += 4
            else:
                break
        except ValueError:
            break

    return fields, offset


def _decode_len(raw: bytes, depth: int, max_depth: int) -> Any:
    if depth < max_depth and len(raw) >= 2:
        nested, consumed = _decode_message(raw, depth=depth + 1, max_depth=max_depth)
        if consumed == len(raw) and _looks_like_nested_message(nested):
            return nested

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw

    if all(ch in "\r\n\t" or 32 <= ord(ch) for ch in text):
        return text
    return raw


def _looks_like_nested_message(fields: list[ProtoField]) -> bool:
    if not fields:
        return False
    if len(fields) == 1 and fields[0].wire_type == 0:
        return False
    return True


def find_fields(fields: list[ProtoField], number: int) -> list[ProtoField]:
    return [field for field in fields if field.number == number]


def first_text(fields: list[ProtoField], number: int) -> str:
    for field in fields:
        if field.number == number and isinstance(field.value, str):
            return field.value
    return ""


def raw_text(fields: list[ProtoField], number: int) -> str:
    """Decode a length-delimited field's raw bytes as UTF-8 text.

    The generic decoder sometimes mis-parses short answer strings as nested
    messages (e.g. b"MANGO" decodes as a valid fixed32 field). Reading the raw
    bytes directly avoids that ambiguity for fields known to hold text.
    """
    for field in fields:
        if field.number == number and field.raw is not None:
            try:
                return field.raw.decode("utf-8")
            except UnicodeDecodeError:
                return ""
    return first_text(fields, number)


def walk_text(fields: list[ProtoField]) -> list[str]:
    texts: list[str] = []
    for field in fields:
        if isinstance(field.value, str):
            texts.append(field.value)
        elif isinstance(field.value, list):
            texts.extend(walk_text(field.value))
    return texts
