#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2

set -euo pipefail

readonly max_files=4096
readonly max_repo_files=200000
readonly -a registry_rulesets=(p/golang p/javascript p/security-audit)
# Excluded HISS-20 fixtures are deliberate violations replayed by make hiss-fixtures.
readonly -a registry_excludes=(.config/hiss/testdata .config/hiss/semgrep/testdata)

raw_list=''
path_list=''

cleanup() {
	if [[ -n "$raw_list" ]]; then
		rm -f -- "$raw_list"
	fi
	if [[ -n "$path_list" ]]; then
		rm -f -- "$path_list"
	fi
}

trap cleanup EXIT

usage() {
	printf 'usage: %s [--files PATH...]\n' "${0##*/}" >&2
}

resolve_root() {
	(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
}

build_scan_args() {
	local item idx=0
	for item in "${registry_rulesets[@]}"; do
		((idx += 1))
		if (( idx > 16 )); then
			break
		fi
		scan_args+=(--config "$item")
	done
	scan_args+=(--error --metrics=off --quiet)
	idx=0
	for item in "${registry_excludes[@]}"; do
		((idx += 1))
		if (( idx > 16 )); then
			break
		fi
		scan_args+=(--exclude "$item")
	done
	scan_args+=(.)
}

write_repo_paths() {
	local root="$1"
	raw_list="$(mktemp "${TMPDIR:-/tmp}/golusoris-semgrep-ls.XXXXXX")"
	git -C "$root" ls-files -z --deduplicate --cached --others --exclude-standard >"$raw_list"
	local path='' count=0
	while IFS= read -r -d '' path; do
		((count += 1))
		if (( count > max_repo_files )); then
			printf 'semgrep-registry: more than %d repository files\n' "$max_repo_files" >&2
			return 1
		fi
		if [[ -e "$root/$path" || -L "$root/$path" ]]; then
			printf '%s\0' "$path"
		fi
	done <"$raw_list" >"$path_list"
	rm -f -- "$raw_list"
	raw_list=''
}

main() {
	local mode='repo'
	if (( $# > 0 )); then
		if [[ "$1" != --files ]]; then
			usage
			return 2
		fi
		shift
		if (( $# == 0 || $# > max_files )); then
			usage
			return 2
		fi
		mode='files'
	fi

	local root
	root="$(resolve_root)"
	readonly root

	# shellcheck source=/dev/null
	. "$root/tools/tool-versions.env"
	local image="semgrep/semgrep:${SEMGREP_VERSION}@${SEMGREP_IMAGE_DIGEST}"
	local docker_cmd="${DOCKER_BIN:-docker}"

	path_list="$(mktemp "${TMPDIR:-/tmp}/golusoris-semgrep-registry.XXXXXX")"
	if [[ "$mode" == files ]]; then
		printf '%s\0' "$@" >"$path_list"
	else
		write_repo_paths "$root" || return $?
	fi

	local -a scan_args=()
	build_scan_args

	local -a statuses
	set +e
	# shellcheck disable=SC2016 # Container sh expands positional parameters, not host bash.
	tar -C "$root" --null -T "$path_list" -cf - \
		| "$docker_cmd" run --rm -i "$image" \
			sh -ec 'mkdir -p /src && tar -xf - -C /src && cd /src && semgrep scan "$@"' \
			sh "${scan_args[@]}"
	statuses=("${PIPESTATUS[@]}")
	set -e
	if (( statuses[0] != 0 )); then
		printf 'semgrep-registry: source stream failed (tar exit %d)\n' "${statuses[0]}" >&2
		return 1
	fi
	if (( statuses[1] != 0 )); then
		printf 'semgrep-registry: registry scan failed with exit status %d\n' "${statuses[1]}" >&2
		return "${statuses[1]}"
	fi
}

main "$@"
