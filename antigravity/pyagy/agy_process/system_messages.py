"""Decode native system-step observations; stdlib-only inside agy's interpreter.

The serializers can revisit the same step while building later prompts. Every
observation is retained, and a step address identifies an object only within its
process lifetime. These are harness notifications, not provider system prompts.
"""
import struct


_HEADER = struct.Struct("<4sIQQII")


def decode_system_message(source, step_address, data):
    """Decode one copied native packet, reporting any deliberate/transport truncation."""
    if len(data) < _HEADER.size:
        raise ValueError("incomplete system-message capture header")
    magic, version, text_bytes, event_bytes, text_copied, event_copied = _HEADER.unpack_from(data)
    if magic != b"AGSM" or version != 1:
        raise ValueError("unsupported system-message capture format")
    if text_copied > text_bytes or event_copied > event_bytes:
        raise ValueError("invalid system-message capture lengths")
    expected = _HEADER.size + event_copied + text_copied
    if len(data) > expected:
        raise ValueError("unexpected trailing system-message capture bytes")
    event = data[_HEADER.size:_HEADER.size + event_copied]
    text = data[_HEADER.size + event_copied:expected]
    return {
        "kind": "system_message",
        "capture_source": source,
        "step_address": step_address,
        "event_type": event.decode("utf-8", "replace"),
        "text": text.decode("utf-8", "replace"),
        "text_bytes": text_bytes,
        "event_type_bytes": event_bytes,
        "captured_text_bytes": len(text),
        "captured_event_type_bytes": len(event),
        "truncated": len(text) != text_bytes or len(event) != event_bytes,
    }
