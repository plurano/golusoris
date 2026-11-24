#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2

set -euo pipefail

readonly max_pushed_paths=4096

is_excluded_path() {
	case "$1" in
		.config/hiss/testdata | .config/hiss/testdata/* | \
		.config/hiss/semgrep/testdata | .config/hiss/semgrep/testdata/*)
			return 0
			;;
	esac
	return 1
}

docker_available() {
	local docker_cmd="${DOCKER_BIN:-docker}"
	command -v "$docker_cmd" >/dev/null 2>&1 || return 1
	"$docker_cmd" info >/dev/null 2>&1
}

select_pushed_files() {
	local root="$1"
	shift
	local path path_idx=0
	for path in "$@"; do
		((path_idx += 1))
		if (( path_idx > max_pushed_paths )); then
			break
		fi
		# Windows Git passes backslash-separated paths in hook arguments.
		path="${path//\\//}"
		[[ ! -L "$root/$path" && -f "$root/$path" ]] || continue
		is_excluded_path "$path" && continue
		if ! git -C "$root" diff --quiet HEAD -- "$path"; then
			printf 'semgrep: %s has uncommitted changes; commit them before pushing\n' "$path" >&2
			return 1
		fi
		selected+=("$path")
	done
}

main() {
	if [[ "${1:-}" != --pushed ]]; then
		printf 'usage: %s --pushed [PATH...]\n' "${0##*/}" >&2
		return 2
	fi
	shift
	if (( $# > max_pushed_paths )); then
		printf 'semgrep: pushed path count exceeds 4096\n' >&2
		return 2
	fi
	local root
	local -a selected=()
	root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)" || return $?
	select_pushed_files "$root" "$@" || return $?
	if (( ${#selected[@]} == 0 )); then
		printf 'semgrep: no pushed files to scan, skipping\n'
		return 0
	fi
	if ! docker_available; then
		printf 'semgrep: docker is not available, skipping; CI security-scan Semgrep remains authoritative\n' >&2
		return 0
	fi
	bash "$root/scripts/ci/semgrep-registry.sh" --files "${selected[@]}" || return $?
}

main "$@"
