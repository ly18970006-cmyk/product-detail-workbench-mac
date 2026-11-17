"""Synchronize PhotoCraft's PSD text styles with Photoshop's document text records."""

import re
import struct


class PSDTextAlignmentError(ValueError):
    """The exported PSD did not have the expected text records."""


def _uint(data, offset):
    return struct.unpack_from(">I", data, offset)[0]


def _tags(data, start, end, alignment):
    while start + 12 <= end:
        signature, tag, length = struct.unpack_from(">4s4sI", data, start)
        start += 12
        if signature != b"8BIM" or start + length > end:
            raise PSDTextAlignmentError("unexpected PSD tagged block")
        yield tag, start, length
        start += (length + alignment - 1) // alignment * alignment


def _layers(data, start, end):
    count = abs(struct.unpack_from(">h", data, start)[0])
    start += 2
    layers = []
    for _ in range(count):
        channels = struct.unpack_from(">H", data, start + 16)[0]
        start += 30 + channels * 6
        stop = start + 4 + _uint(data, start)
        if stop > end:
            raise PSDTextAlignmentError("unexpected PSD layer record")
        offset = start + 4
        offset += 4 + _uint(data, offset)  # Layer mask
        offset += 4 + _uint(data, offset)  # Blending ranges
        offset += (data[offset] + 4) // 4 * 4  # Pascal name
        layers.append({tag: (offset, length) for tag, offset, length in _tags(data, offset, stop, 2)})
        start = stop
    return layers


def _text_indices(data, layers, paths):
    stack, found = [], {}
    for layer in reversed(layers):
        section = _uint(data, layer[b"lsct"][0]) if b"lsct" in layer else 0
        if section == 3:
            if not stack:
                raise PSDTextAlignmentError("unexpected PSD layer group")
            stack.pop()
            continue
        if b"luni" not in layer:
            if section in (1, 2):
                raise PSDTextAlignmentError("unnamed PSD layer group")
            continue
        name_at, size = layer[b"luni"]
        name = data[name_at + 4:name_at + size].decode("utf-16-be").rstrip("\0")
        if section in (1, 2):
            stack.append(name)
            continue
        path = (*stack, name)
        if path not in paths:
            continue
        if path in found or b"TySh" not in layer:
            raise PSDTextAlignmentError("non-unique PSD text layer")
        offset, size = layer[b"TySh"]
        local = data[offset:offset + size]
        index = re.search(rb"\x00\x00\x00\x09TextIndexlong(.{4})", local, re.DOTALL)
        editor = re.search(rb"/Editor\s*<<\s*/Text\s*\((.*?)\)\s*>>", local, re.DOTALL)
        paragraphs = re.search(rb"/ParagraphRun\s*<<(.*?)(?=/StyleRun)", local, re.DOTALL)
        if not index or not editor or not paragraphs:
            raise PSDTextAlignmentError("unexpected PSD text descriptor")
        alignment = re.findall(rb"/Justification\s+(\d+)", paragraphs[1])
        if not alignment or any(value != b"1" for value in alignment):
            raise PSDTextAlignmentError("missing right-aligned text paragraph")
        found[path] = (_uint(index[1], 0), editor[1] + b")", len(alignment))
    if stack or found.keys() != paths or len({record[0] for record in found.values()}) != len(paths):
        raise PSDTextAlignmentError("missing or overlapping PSD text indices")
    return found.values()


def sync_right_alignment(path, layer_paths):
    """Update only the global paragraph justification bytes for these edited layers."""
    data = path.read_bytes()
    try:
        if data[:6] != b"8BPS\x00\x01":
            raise PSDTextAlignmentError("unsupported PSD version")
        offset = 26
        offset += 4 + _uint(data, offset)  # Color mode
        offset += 4 + _uint(data, offset)  # Image resources
        end = offset + 4 + _uint(data, offset)
        start = offset + 8
        layer_end = start + _uint(data, offset + 4)
        mask_end = layer_end + 4 + _uint(data, layer_end)
        if not start <= layer_end < mask_end <= end <= len(data):
            raise PSDTextAlignmentError("unexpected PSD layer and mask length")
        layers = _layers(data, start, layer_end)
        indices = _text_indices(data, layers, {tuple(path) for path in layer_paths})
        text_tags = [(offset, length) for tag, offset, length in _tags(data, mask_end, end, 4)
                     if tag == b"Txt2"]
        if len(text_tags) != 1:
            raise PSDTextAlignmentError("unexpected document text metadata")
        offset, length = text_tags[0]
        engine = data[offset:offset + length]
        # PhotoCraft indents top-level text entries two tabs in its Txt2 serializer.
        records = list(re.finditer(rb"\n\t\t<<\s*/0\s*<<\s*/0\s*\(", engine))
        patch_offsets = []
        for index, text, paragraph_count in indices:
            start = records[index].end()
            stop = records[index + 1].start() if index + 1 < len(records) else length
            if not engine.startswith(text, start, stop):
                raise PSDTextAlignmentError("document and layer text do not match")
            start += len(text)
            paragraph = re.match(rb"\s*/5\s*<<.*?(?=\n\t{4}/6\b)", engine[start:stop], re.DOTALL)
            if not paragraph:
                raise PSDTextAlignmentError("missing document paragraph styles")
            values = list(re.finditer(rb"/5\s*<<\s*/0\s+([01])(?=\s+/1\b)", paragraph[0]))
            if len(values) != paragraph_count:
                raise PSDTextAlignmentError("unexpected number of document paragraph styles")
            patch_offsets.extend(offset + start + value.start(1) for value in values if value[1] == b"0")
    except (struct.error, UnicodeError, IndexError) as error:
        raise PSDTextAlignmentError("invalid PSD text metadata") from error
    if patch_offsets:
        with path.open("r+b") as target:
            for offset in patch_offsets:
                target.seek(offset)
                target.write(b"1")
