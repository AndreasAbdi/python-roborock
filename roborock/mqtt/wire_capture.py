"""Utilities for capturing raw MQTT frames for replay/debugging."""

from __future__ import annotations

import base64
import datetime as dt
import json
import pathlib
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class WireCaptureEvent:
    """A raw MQTT frame captured on the wire."""

    direction: str
    topic: str
    payload: bytes

    def as_json(self) -> str:
        return json.dumps(
            {
                "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                "direction": self.direction,
                "topic": self.topic,
                "payload_b64": base64.b64encode(self.payload).decode(),
            },
            separators=(",", ":"),
        )


class WireCapture:
    """Appends MQTT frames to a JSONL trace file."""

    def __init__(self, path: str | pathlib.Path):
        self._path = pathlib.Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, direction: str, topic: str, payload: bytes) -> None:
        event = WireCaptureEvent(direction=direction, topic=topic, payload=payload)
        line = event.as_json()
        with self._lock:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.write("\n")
