# SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2

"""Summarize recovered faults or failed tests from a go test -json stream."""

from __future__ import annotations

import io
import json
import sys
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import TextIO


@dataclass(frozen=True)
class Config:
    """CLI configuration for go test -json event summarization."""

    mode: str
    marker: str
    max_output_lines: int
    max_failed_tests: int
    max_tracked_tests: int


@dataclass
class FailureTracker:
    """Mutable state while scanning a go test -json stream for test failures."""

    cfg: Config
    out: TextIO
    outputs: OrderedDict[tuple[str, str], deque[str]]
    shown: int = 0
    extra: int = 0


def parse_config(argv: list[str]) -> Config:
    """Parse and validate CLI arguments into a Config value."""
    if len(argv) != 5:
        raise ValueError(
            "usage: go_test_events.py <faults|failures> <marker> <max_out> <max_fail> <max_track>"
        )
    mode, marker = argv[0], argv[1]
    if mode not in ("faults", "failures"):
        raise ValueError(f"fault-recovery: unknown summary mode: {mode}")
    max_output_lines = int(argv[2])
    max_failed_tests = int(argv[3])
    max_tracked_tests = int(argv[4])
    if max_output_lines < 0 or max_failed_tests < 0 or max_tracked_tests < 0:
        raise ValueError("fault-recovery: bounds must be non-negative")
    return Config(
        mode=mode,
        marker=marker,
        max_output_lines=max_output_lines,
        max_failed_tests=max_failed_tests,
        max_tracked_tests=max_tracked_tests,
    )


def parse_event(line: str) -> dict[str, object] | None:
    """Decode one JSON object line from go test -json, ignoring malformed lines."""
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def event_key(event: dict[str, object]) -> tuple[str, str]:
    """Extract the (package, test) identifier pair from a decoded event."""
    package = str(event.get("Package") or "?")
    test = str(event.get("Test") or "<package>")
    return package, test


def summarize_faults(stream: TextIO, marker: str, out: TextIO) -> None:
    """Write sorted recovered-fault hits found in stream to out."""
    hits: set[tuple[str, str]] = set()
    for line in stream:
        event = parse_event(line)
        if event is None or event.get("Action") != "output":
            continue
        raw_output = event.get("Output")
        if isinstance(raw_output, str) and marker in raw_output:
            hits.add(event_key(event))
    for package, test in sorted(hits):
        out.write(f"{package} (seen in {test})\n")


def append_output(
    outputs: OrderedDict[tuple[str, str], deque[str]],
    key: tuple[str, str],
    output: str,
    cfg: Config,
) -> None:
    """Append event output lines to the per-test ring buffer with FIFO cap."""
    if cfg.max_tracked_tests == 0:
        return
    buf = outputs.get(key)
    if buf is None:
        if len(outputs) >= cfg.max_tracked_tests:
            outputs.popitem(last=False)
        buf = deque(maxlen=cfg.max_output_lines)
        outputs[key] = buf
    for raw_line in output.removesuffix("\n").split("\n"):
        buf.append(raw_line.rstrip("\r"))


def write_failed_test(
    key: tuple[str, str], lines: deque[str] | tuple[str, ...], out: TextIO
) -> None:
    """Write one failed-test header and its retained output tail to out."""
    package, test = key
    out.write(f"FAIL {package} {test}\n")
    for out_line in lines:
        out.write(f"    {out_line}\n")


def record_failure_event(tracker: FailureTracker, event: dict[str, object]) -> None:
    """Update failure tracker state from one decoded go test -json event."""
    action = event.get("Action")
    key = event_key(event)
    if action == "output":
        raw_output = event.get("Output")
        if isinstance(raw_output, str) and raw_output:
            append_output(tracker.outputs, key, raw_output, tracker.cfg)
        return
    if action in ("pass", "skip"):
        tracker.outputs.pop(key, None)
        return
    if action == "fail":
        lines = tracker.outputs.pop(key, ())
        if tracker.shown < tracker.cfg.max_failed_tests:
            tracker.shown += 1
            write_failed_test(key, lines, tracker.out)
        else:
            tracker.extra += 1


def summarize_failures(stream: TextIO, cfg: Config, out: TextIO) -> None:
    """Write bounded failed-test headers, output tails, and overflow count to out."""
    tracker = FailureTracker(cfg=cfg, out=out, outputs=OrderedDict())
    for line in stream:
        event = parse_event(line)
        if event is not None:
            record_failure_event(tracker, event)
    if tracker.extra > 0:
        out.write(f"... {tracker.extra} more failures not shown\n")


def main(argv: list[str]) -> int:
    """Run the requested go test -json summary mode over standard input."""
    try:
        cfg = parse_config(argv)
    except ValueError as err:
        sys.stderr.write(f"{err}\n")
        return 3
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(newline="\n")
    if cfg.mode == "faults":
        summarize_faults(sys.stdin, cfg.marker, sys.stdout)
    else:
        summarize_failures(sys.stdin, cfg, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
