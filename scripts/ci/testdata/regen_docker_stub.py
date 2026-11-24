#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Stand-in for docker run ... uv pip compile in regenerate.py selftests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

MAX_ARGS = 64
MAX_MOUNT_FIELDS = 8
MAX_LINES = 256
MAX_PACKAGES = 256
MAX_VERSIONS = 64
MAX_REQUIRES = 64
CUTOFF_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
RUN_VALUE_OPTIONS = frozenset({"--user", "--env", "--mount", "--workdir"})
COMPILE_FLAGS = frozenset({"--quiet", "--generate-hashes", "--no-emit-index-url"})
COMPILE_VALUE_OPTIONS = frozenset(
    {
        "--exclude-newer",
        "--exclude-newer-package",
        "--python-version",
        "--output-file",
    }
)
REQUIREMENT_PIN_PATTERN = (
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[[A-Za-z0-9._,-]+\])?==(?P<version>[A-Za-z0-9.+!_-]+)"
)
REQUIREMENT_PIN = re.compile(REQUIREMENT_PIN_PATTERN)


class StubError(Exception):
    """Raised when CLI shape (exit 2) or package resolution (exit 1) fails."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


class CompileRequest(NamedTuple):
    """Parsed docker run and uv pip compile arguments."""

    src_dir: Path
    cutoff: str
    exclude_pkg: str
    python_version: str
    output_file: str
    input_file: str


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_timestamp(value: str) -> datetime:
    try:
        return datetime.strptime(value, CUTOFF_FORMAT)
    except ValueError as error:
        raise StubError(f"invalid timestamp {value!r}", 2) from error


def parse_mount_src(mount: str) -> Path:
    parts = mount.split(",")
    if len(parts) > MAX_MOUNT_FIELDS:
        raise StubError("too many --mount fields", 2)
    fields: dict[str, str] = {}
    for i in range(min(len(parts), MAX_MOUNT_FIELDS)):
        field, sep, value = parts[i].partition("=")
        if not sep or not field or field in fields:
            raise StubError(f"malformed --mount field: {parts[i]!r}", 2)
        fields[field] = value
    if fields.get("type") != "bind" or fields.get("dst") != "/src":
        raise StubError(f"unsupported --mount specification: {mount!r}", 2)
    src = fields.get("src", "")
    if not src or not Path(src).is_dir():
        raise StubError(f"missing --mount src directory: {src!r}", 2)
    return Path(src)


def consume_run_option(args: list[str], idx: int) -> tuple[int, str | None]:
    arg = args[idx]
    if arg == "--rm":
        return idx + 1, None
    if arg in RUN_VALUE_OPTIONS and idx + 1 < len(args):
        return idx + 2, (args[idx + 1] if arg == "--mount" else None)
    raise StubError(f"unsupported or incomplete docker run option: {arg}", 2)


def parse_run_segment(args: list[str]) -> tuple[str, list[str]]:
    if len(args) > MAX_ARGS or args[:1] != ["run"]:
        raise StubError("expected: docker run ... <image> uv pip compile ...", 2)
    mount = ""
    idx = 1
    for _ in range(min(len(args), MAX_ARGS)):
        if idx >= len(args):
            break
        if not args[idx].startswith("-"):
            if not mount:
                raise StubError("docker run missing --mount", 2)
            return mount, args[idx + 1 :]
        idx, new_mount = consume_run_option(args, idx)
        if new_mount is not None:
            mount = new_mount
    raise StubError("docker run missing image argument", 2)


def consume_compile_option(
    tail: list[str], idx: int, options: dict[str, str]
) -> int:
    arg = tail[idx]
    if arg in COMPILE_FLAGS:
        return idx + 1
    if arg in COMPILE_VALUE_OPTIONS and idx + 1 < len(tail):
        options[arg] = tail[idx + 1]
        return idx + 2
    raise StubError(f"unsupported or incomplete compile option: {arg}", 2)


def parse_compile_segment(mount: str, tail: list[str]) -> CompileRequest:
    if len(tail) > MAX_ARGS or tail[:3] != ["uv", "pip", "compile"]:
        raise StubError("expected uv pip compile after image", 2)
    options: dict[str, str] = {}
    inputs: list[str] = []
    idx = 3
    for _ in range(min(len(tail), MAX_ARGS)):
        if idx >= len(tail):
            break
        if tail[idx].startswith("-"):
            idx = consume_compile_option(tail, idx, options)
        else:
            inputs.append(tail[idx])
            idx += 1
    if len(inputs) != 1 or COMPILE_VALUE_OPTIONS - options.keys():
        raise StubError("compile requires all value options and one input", 2)
    return CompileRequest(
        src_dir=parse_mount_src(mount),
        cutoff=options["--exclude-newer"],
        exclude_pkg=options["--exclude-newer-package"],
        python_version=options["--python-version"],
        output_file=options["--output-file"],
        input_file=inputs[0],
    )


def parse_index_versions(
    versions: object,
) -> list[tuple[str, datetime, list[str]]]:
    if not isinstance(versions, list) or len(versions) > MAX_VERSIONS:
        raise StubError("invalid version list in REGEN_STUB_INDEX", 2)
    parsed: list[tuple[str, datetime, list[str]]] = []
    for i in range(min(len(versions), MAX_VERSIONS)):
        entry = versions[i]
        if (
            not isinstance(entry, list)
            or len(entry) != 3
            or not isinstance(entry[2], list)
        ):
            raise StubError("invalid version tuple in REGEN_STUB_INDEX", 2)
        reqs = [
            normalize_name(str(entry[2][j]))
            for j in range(min(len(entry[2]), MAX_REQUIRES))
        ]
        parsed.append((str(entry[0]), parse_timestamp(str(entry[1])), reqs))
    return parsed


def load_index() -> dict[str, list[tuple[str, datetime, list[str]]]]:
    index_file = os.environ.get("REGEN_STUB_INDEX", "")
    if not index_file:
        raise StubError("REGEN_STUB_INDEX is not set", 2)
    try:
        raw = json.loads(Path(index_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise StubError(f"could not read REGEN_STUB_INDEX: {error}", 2) from error
    if not isinstance(raw, dict) or len(raw) > MAX_PACKAGES:
        raise StubError("REGEN_STUB_INDEX must be a JSON object", 2)
    index: dict[str, list[tuple[str, datetime, list[str]]]] = {}
    items = list(raw.items())
    for i in range(min(len(items), MAX_PACKAGES)):
        pkg_name, versions = items[i]
        index[normalize_name(str(pkg_name))] = parse_index_versions(versions)
    return index


def safe_src_child(src_dir: Path, rel: str) -> Path:
    rel_path = Path(rel)
    if rel_path.is_absolute() or rel_path.anchor or ".." in rel_path.parts:
        raise StubError(f"unsafe path {rel!r}", 2)
    return src_dir / rel_path


def read_direct_requirements(path: Path) -> list[tuple[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise StubError(f"could not read {path}: {error}", 2) from error
    if len(lines) > MAX_LINES:
        raise StubError(f"{path} exceeds {MAX_LINES} lines", 2)
    pins: list[tuple[str, str]] = []
    for i in range(min(len(lines), MAX_LINES)):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = REQUIREMENT_PIN.fullmatch(stripped)
        if match is None:
            raise StubError(f"unsupported requirement: {stripped!r}", 1)
        pins.append((normalize_name(match["name"]), match["version"]))
    return pins


def find_direct_entry(
    name: str,
    version: str,
    cutoff: datetime,
    entries: list[tuple[str, datetime, list[str]]],
) -> list[str]:
    for i in range(min(len(entries), MAX_VERSIONS)):
        ver, uploaded, reqs = entries[i]
        if ver == version and uploaded <= cutoff:
            return reqs
    raise StubError(f"{name} was filtered by exclude-newer", 1)


def pick_transitive_version(
    name: str,
    cutoff: datetime,
    entries: list[tuple[str, datetime, list[str]]],
) -> str:
    chosen: str | None = None
    for i in range(min(len(entries), MAX_VERSIONS)):
        ver, uploaded, _reqs = entries[i]
        if uploaded <= cutoff:
            chosen = ver
    if chosen is None:
        raise StubError(f"{name} was filtered by exclude-newer", 1)
    return chosen


def resolve_packages(
    direct: list[tuple[str, str]],
    cutoff: datetime,
    index: dict[str, list[tuple[str, datetime, list[str]]]],
) -> dict[str, str]:
    resolved = dict(direct)
    transitive: dict[str, None] = {}
    for i in range(min(len(direct), MAX_LINES)):
        name, version = direct[i]
        reqs = find_direct_entry(name, version, cutoff, index.get(name, []))
        for j in range(min(len(reqs), MAX_REQUIRES)):
            if reqs[j] not in resolved:
                transitive[reqs[j]] = None
    trans_names = list(transitive.keys())
    for i in range(min(len(trans_names), MAX_PACKAGES)):
        req_name = trans_names[i]
        resolved[req_name] = pick_transitive_version(
            req_name, cutoff, index.get(req_name, [])
        )
    return resolved


def format_locked_package(name: str, version: str) -> str:
    pin = f"{name}=={version}"
    digest = hashlib.sha256(pin.encode("utf-8")).hexdigest()
    return f"{pin} \\\n    --hash=sha256:{digest}\n"


def write_lock_output(req: CompileRequest, resolved: dict[str, str]) -> None:
    out_path = safe_src_child(req.src_dir, req.output_file)
    header = (
        "# This file was autogenerated by uv via the following command:\n"
        "#    uv pip compile --generate-hashes --no-emit-index-url "
        f"--exclude-newer {req.cutoff} "
        f"--exclude-newer-package {req.exclude_pkg} "
        f"--python-version {req.python_version} "
        f"--output-file {req.output_file} {req.input_file}\n"
    )
    body = "".join(
        format_locked_package(name, resolved[name])
        for name in sorted(resolved)[:MAX_PACKAGES]
    )
    out_path.write_text(header + body, encoding="utf-8")


def main() -> int:
    try:
        mount, tail = parse_run_segment(sys.argv[1:])
        req = parse_compile_segment(mount, tail)
        cutoff_dt = parse_timestamp(req.cutoff)
        index = load_index()
        input_path = safe_src_child(req.src_dir, req.input_file)
        direct = read_direct_requirements(input_path)
        resolved = resolve_packages(direct, cutoff_dt, index)
        write_lock_output(req, resolved)
    except StubError as error:
        print(error, file=sys.stderr)
        return error.code
    return 0


if __name__ == "__main__":
    sys.exit(main())
