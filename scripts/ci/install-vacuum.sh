#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2

# Install the pinned vacuum OpenAPI linter only after its module zip matches the
# go.sum hash recorded in tools/tool-versions.env.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly repo_root
readonly module='github.com/daveshanley/vacuum'
input_record=''

usage() {
	printf 'usage: %s <install-dir>\n' "${0##*/}" >&2
	printf '       %s --cache-key\n' "${0##*/}" >&2
}

load_and_validate_pins() {
	# shellcheck source=/dev/null
	. "$repo_root/tools/tool-versions.env"

	if [[ ! "$VACUUM_VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
		printf 'invalid VACUUM_VERSION: %s\n' "$VACUUM_VERSION" >&2
		exit 1
	fi
	if [[ ! "$VACUUM_MODULE_SUM" =~ ^h1:[A-Za-z0-9+/]{43}=$ ]]; then
		printf 'invalid pinned module sum: %s\n' "$VACUUM_MODULE_SUM" >&2
		exit 1
	fi
}

# Bash 3.2 (macOS runners): no mapfile, no case-changing expansions.
# Prints every input that decides the build, one key=value line each, in a fixed order.
vacuum_input_record() {
	local -a names=(GOVERSION GOOS GOARCH GOAMD64 GOARM64 CGO_ENABLED GOFLAGS GOEXPERIMENT)
	local -a values=()
	local go_env i line count=0
	local max_lines
	# From a module-free directory, as the build runs, so a caller's go.mod cannot switch toolchains;
	# the end marker keeps trailing empty values.
	max_lines=$((${#names[@]} + 2))
	go_env="$(cd / && go env "${names[@]}" && printf 'end\n')" || return 1
	while IFS= read -r line && ((count < max_lines)); do
		values+=("$line")
		count=$((count + 1))
	done <<<"$go_env"
	if ((${#values[@]} != ${#names[@]} + 1)) || [[ "${values[${#names[@]}]}" != 'end' ]]; then
		printf 'go env printed %d lines for %d variables\n' "${#values[@]}" "${#names[@]}" >&2
		return 1
	fi
	printf 'module=%s\nversion=%s\nmodule_sum=%s\n' "$module" "$VACUUM_VERSION" "$VACUUM_MODULE_SUM"
	for i in "${!names[@]}"; do
		printf 'go_%s=%s\n' "$(printf '%s' "${names[$i]}" | tr '[:upper:]' '[:lower:]')" "${values[$i]}"
	done
}

# Names the platform for readability; the digest covers the whole record.
vacuum_cache_key() {
	local record goos goarch digest
	record="$(vacuum_input_record)" || return 1
	goos="$(sed -n 's/^go_goos=//p' <<<"$record")"
	goarch="$(sed -n 's/^go_goarch=//p' <<<"$record")"
	digest="$(printf '%s\n' "$record" | sha256sum)" || return 1
	printf 'vacuum-%s-%s-%s\n' "$goos" "$goarch" "${digest:0:32}"
}

# Clears a rejected entry so the save after the rebuild starts from nothing.
cache_reject() {
	printf '::warning::vacuum cache entry rejected: %s; rebuilding from the pinned module\n' "$1" >&2
	rm -rf -- "$2/vacuum" "$2/vacuum.provenance"
}

cache_entry_regular() {
	[[ -f "$1/vacuum" && ! -L "$1/vacuum" && -f "$1/vacuum.provenance" && ! -L "$1/vacuum.provenance" ]]
}

cache_provenance_matches_inputs() {
	cmp -s <(printf '%s\n' "$input_record") <(sed '$d' "$1/vacuum.provenance")
}

cache_digest_matches_provenance() {
	local last digest
	last="$(tail -n 1 "$1/vacuum.provenance")" || return 1
	[[ "$last" =~ ^sha256=[0-9a-f]{64}$ ]] || return 1
	digest="$(sha256sum "$1/vacuum")" || return 1
	[[ "${last#sha256=}" == "${digest%% *}" ]]
}

cache_build_info_matches_pin() {
	local want info
	want=$'\tmod\t'"$module"$'\t'"$VACUUM_VERSION"$'\t'"$VACUUM_MODULE_SUM"
	info="$(cd / && go version -m "$1/vacuum")" || return 1
	grep -Fxq -- "$want" <<<"$info"
}

# Runs the cached binary once; a failing run reports its exit status instead of a version.
cache_binary_version() {
	local got
	got="$("$1/vacuum" version)" || got="(exit $?)"
	printf '%s' "$got"
}

# Checks the entry in a fixed order and stops at the first failed binding.
vacuum_try_cache_restore() {
	local dir="$1" reason='' got
	if [[ ! -e "$dir/vacuum" && ! -L "$dir/vacuum" && ! -e "$dir/vacuum.provenance" && ! -L "$dir/vacuum.provenance" ]]; then
		printf 'vacuum cache: no entry; installing from the pinned module\n' >&2
		return 1
	fi
	if ! cache_entry_regular "$dir"; then
		reason='entry is not a regular file'
	elif ! cache_provenance_matches_inputs "$dir"; then
		reason='provenance does not match the pinned inputs'
	elif ! cache_digest_matches_provenance "$dir"; then
		reason='binary digest does not match its provenance'
	elif ! cache_build_info_matches_pin "$dir"; then
		reason='build info does not name the pinned module sum'
	else
		got="$(cache_binary_version "$dir")"
		[[ "$got" == "$VACUUM_VERSION" ]] || reason="binary reports version $got"
	fi
	if [[ -n "$reason" ]]; then
		cache_reject "$reason" "$dir"
		return 1
	fi
}

# Writes the provenance last and atomically, so a partial save never verifies.
vacuum_save_cache() {
	local dir="$1" binary="$2" tmp digest
	install -m 0755 "$binary" "$dir/vacuum"
	digest="$(sha256sum "$dir/vacuum")"
	tmp="$(mktemp "$dir/vacuum.provenance.XXXXXX")"
	printf '%s\nsha256=%s\n' "$input_record" "${digest%% *}" >"$tmp"
	mv -f "$tmp" "$dir/vacuum.provenance"
}

if (($# == 1)) && [[ "$1" == '--cache-key' ]]; then
	load_and_validate_pins
	vacuum_cache_key
	exit 0
fi

if (($# != 1)) || [[ -z "$1" ]]; then
	usage
	exit 2
fi
install_dir="$(mkdir -p "$1" && cd "$1" && pwd)"
readonly install_dir

load_and_validate_pins

cache_dir=''
if [[ -n "${VACUUM_CACHE_DIR:-}" ]]; then
	# Absolute, because the build below changes directory before the save.
	cache_dir="$(mkdir -p "$VACUUM_CACHE_DIR" && cd "$VACUUM_CACHE_DIR" && pwd)"
	input_record="$(vacuum_input_record)"
	if vacuum_try_cache_restore "$cache_dir"; then
		install -m 0755 "$cache_dir/vacuum" "$install_dir/vacuum"
		printf 'restored verified vacuum %s from cache: %s\n' "$VACUUM_VERSION" "$install_dir/vacuum"
		exit 0
	fi
fi
readonly cache_dir input_record

# Outside any module, so the caller's go.mod cannot redirect the download.
work_dir="$(mktemp -d "${TMPDIR:-/tmp}/golusoris-vacuum.XXXXXX")"
readonly work_dir
trap 'rm -rf "$work_dir"' EXIT
cd "$work_dir"

download="$(GOFLAGS=-mod=mod GO111MODULE=on bash "$repo_root/scripts/ci/retry.sh" go mod download -json "$module@$VACUUM_VERSION")"
module_sum="$(jq -r '.Sum // ""' <<<"$download")"
if [[ "$module_sum" != "$VACUUM_MODULE_SUM" ]]; then
	printf 'module sum mismatch for %s@%s: got %s, want %s\n' \
		"$module" "$VACUUM_VERSION" "${module_sum:-none}" "$VACUUM_MODULE_SUM" >&2
	exit 1
fi

GOBIN="$work_dir/bin" bash "$repo_root/scripts/ci/retry.sh" go install "$module@$VACUUM_VERSION"
binary="$work_dir/bin/vacuum"
installed_version="$("$binary" version)"
if [[ "$installed_version" != "$VACUUM_VERSION" ]]; then
	printf 'vacuum reports version %s, want %s\n' "$installed_version" "$VACUUM_VERSION" >&2
	exit 1
fi

install -m 0755 "$binary" "$install_dir/vacuum"
printf 'installed verified vacuum %s: %s\n' "$VACUUM_VERSION" "$install_dir/vacuum"

if [[ -n "$cache_dir" ]]; then
	vacuum_save_cache "$cache_dir" "$install_dir/vacuum"
fi
