#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2

# Offline policy test for install-vacuum.sh: a fake go serves the module sum and
# the built binary, so sum, version and pin-shape failures are proven.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly repo_root
suite_root="$(mktemp -d "${TMPDIR:-/tmp}/golusoris-install-vacuum.XXXXXX")"
readonly suite_root
trap 'rm -rf "$suite_root"' EXIT

readonly version='v0.33.1'
readonly good_sum='h1:dlKjyVR7XeJfAGtseGARiJZpD9d6wYWaPTAeW0AgMDU='
readonly other_sum='h1:PQ/1K4axoQon95CUSkTzsHvafj/Go2kjUu2pir7VojY='
readonly cache_dir="$suite_root/cache"
# The pre-cache cases must not inherit a caller's cache or fake-go switches.
unset VACUUM_CACHE_DIR FAKE_NO_BUILD FAKE_GOVERSION

fake_bin="$suite_root/bin"
mkdir -p "$fake_bin" "$suite_root/repo/scripts/ci" "$suite_root/repo/tools" "$cache_dir"
install -m 0755 "$repo_root/scripts/ci/install-vacuum.sh" \
	"$suite_root/repo/scripts/ci/install-vacuum.sh"
install -m 0755 "$repo_root/scripts/ci/retry.sh" \
	"$suite_root/repo/scripts/ci/retry.sh"

cat >"$fake_bin/go" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$GO_LOG"
if (("${FAKE_NO_BUILD:-0}" == 1)) && [[ "$1" == "mod" || "$1" == "install" ]]; then
	printf 'build not allowed\n' >&2
	exit 3
fi
case "$1" in
env)
	shift
	for var in "$@"; do
		case "$var" in
		GOVERSION) printf '%s\n' "${FAKE_GOVERSION:-go1.27.4}" ;;
		GOOS) printf 'linux\n' ;;
		GOARCH) printf 'amd64\n' ;;
		*) printf '\n' ;;
		esac
	done
	;;
mod)
	case "$2" in
	download)
		if (("${FAKE_FLAKY:-0}" == 1)); then
			flaky_count_file="${GO_LOG}.flaky"
			count=0
			if [[ -f "$flaky_count_file" ]]; then
				count="$(cat "$flaky_count_file")"
			fi
			count=$((count + 1))
			printf '%d' "$count" >"$flaky_count_file"
			if ((count == 1)); then
				printf '{"Path":"github.com/daveshanley/vacuum","Error":"transient"}\n'
				exit 1
			fi
		fi
		printf '{"Path":"github.com/daveshanley/vacuum","Sum":"%s"}\n' "$FAKE_SUM"
		;;
	*) exit 2 ;;
	esac
	;;
install)
	mkdir -p "$GOBIN"
	{
		printf '#!/bin/sh\n'
		printf '# buildinfo %s %s\n' "$FAKE_REPORTED" "$FAKE_SUM"
		printf 'printf "%%s\\n" "%s"\n' "$FAKE_REPORTED"
	} >"$GOBIN/vacuum"
	chmod +x "$GOBIN/vacuum"
	;;
version)
	if [[ "$2" != "-m" ]] || [[ -z "${3:-}" ]]; then
		exit 2
	fi
	bin_path="$3"
	if [[ ! -f "$bin_path" ]]; then
		printf 'go: cannot find main module for %s\n' "$bin_path" >&2
		exit 1
	fi
	buildinfo="$(grep '^# buildinfo ' "$bin_path" || true)"
	if [[ -z "$buildinfo" ]]; then
		printf 'go: no build info for %s\n' "$bin_path" >&2
		exit 1
	fi
	read -r ver sum <<<"${buildinfo#\# buildinfo }"
	printf '%s: %s\n' "$bin_path" "${FAKE_GOVERSION:-go1.27.4}"
	printf '\t%s\tgithub.com/daveshanley/vacuum\n' "$bin_path"
	printf '\tmod\tgithub.com/daveshanley/vacuum\t%s\t%s\n' "$ver" "$sum"
	;;
*) exit 2 ;;
esac
FAKE
chmod +x "$fake_bin/go"

write_pins() {
	printf 'VACUUM_VERSION=%s\nVACUUM_MODULE_SUM=%s\n' "$1" "$2" \
		>"$suite_root/repo/tools/tool-versions.env"
}

run_installer() {
	local sum="$1" reported="$2" target="$3"
	env PATH="$fake_bin:$PATH" FAKE_SUM="$sum" FAKE_REPORTED="$reported" \
		FAKE_FLAKY="${FAKE_FLAKY:-0}" RETRY_DELAY_SECONDS="${RETRY_DELAY_SECONDS:-0}" \
		FAKE_NO_BUILD="${FAKE_NO_BUILD:-0}" FAKE_GOVERSION="${FAKE_GOVERSION:-}" \
		VACUUM_CACHE_DIR="${VACUUM_CACHE_DIR:-}" \
		GO_LOG="$suite_root/go.log" \
		bash "$suite_root/repo/scripts/ci/install-vacuum.sh" "$target"
}

expect_failure() {
	local pattern="$1" target="$2"
	shift 2
	local output status=0
	output="$("$@" 2>&1)" || status=$?
	if ((status == 0)) || [[ "$output" != *"$pattern"* ]]; then
		printf 'expected failure containing %q, got status %d: %s\n' \
			"$pattern" "$status" "$output" >&2
		return 1
	fi
	if [[ -e "$target/vacuum" ]]; then
		printf 'failed install left a binary behind: %s\n' "$target/vacuum" >&2
		return 1
	fi
}

# Positive: the pinned sum admits the module and installs the binary.
write_pins "$version" "$good_sum"
run_installer "$good_sum" "$version" "$suite_root/install-ok" >/dev/null
[[ "$("$suite_root/install-ok/vacuum")" == "$version" ]] || {
	printf 'installed vacuum does not report %s\n' "$version" >&2
	exit 1
}
grep -Fxq "install github.com/daveshanley/vacuum@$version" "$suite_root/go.log" || {
	printf 'installer did not build the pinned module version\n' >&2
	exit 1
}

# Negative: another module sum rejects the download before any build.
: >"$suite_root/go.log"
expect_failure 'module sum mismatch' "$suite_root/install-sum" \
	run_installer "$other_sum" "$version" "$suite_root/install-sum"
if grep -q '^install ' "$suite_root/go.log"; then
	printf 'installer built a module whose sum did not match\n' >&2
	exit 1
fi

# Negative: a missing sum in the download record is a mismatch, not a pass.
expect_failure 'got none' "$suite_root/install-nosum" \
	run_installer '' "$version" "$suite_root/install-nosum"

# Negative: a malformed sum pin fails before any download.
write_pins "$version" 'sha256:not-a-sum'
expect_failure 'invalid pinned module sum' "$suite_root/install-pin" \
	run_installer "$good_sum" "$version" "$suite_root/install-pin"

# Negative: a verified module whose binary reports another version.
write_pins "$version" "$good_sum"
expect_failure 'reports version v0.33.0' "$suite_root/install-version" \
	run_installer "$good_sum" v0.33.0 "$suite_root/install-version"

# Boundary: a version pin without the v prefix is refused.
write_pins '0.33.1' "$good_sum"
expect_failure 'invalid VACUUM_VERSION' "$suite_root/install-semver" \
	run_installer "$good_sum" "$version" "$suite_root/install-semver"

# Positive: flaky download succeeds on retry without corrupted stdout.
: >"$suite_root/go.log"
rm -f "$suite_root/go.log.flaky"
write_pins "$version" "$good_sum"
FAKE_FLAKY=1 run_installer "$good_sum" "$version" "$suite_root/install-flaky" >/dev/null
[[ "$("$suite_root/install-flaky/vacuum")" == "$version" ]] || {
	printf 'flaky install: installed vacuum does not report %s\n' "$version" >&2
	exit 1
}
grep -Fxq "install github.com/daveshanley/vacuum@$version" "$suite_root/go.log" || {
	printf 'flaky install: installer did not build the pinned module version\n' >&2
	exit 1
}

cache_key() {
	env PATH="$fake_bin:$PATH" FAKE_GOVERSION="${FAKE_GOVERSION:-}" GO_LOG="$suite_root/go.log" \
		bash "$suite_root/repo/scripts/ci/install-vacuum.sh" --cache-key
}

provenance_digest_matches() {
	local digest
	digest="$(sha256sum "$cache_dir/vacuum")"
	[[ "$(tail -n 1 "$cache_dir/vacuum.provenance")" == "sha256=${digest%% *}" ]]
}

# V1 cold: an empty cache builds, then stores the binary with its provenance.
rm -rf "${cache_dir:?}"/*
write_pins "$version" "$good_sum"
: >"$suite_root/go.log"
VACUUM_CACHE_DIR="$cache_dir" run_installer "$good_sum" "$version" "$suite_root/install-v1" >/dev/null || {
	printf 'V1: cold install failed\n' >&2
	exit 1
}
grep -Fxq "install github.com/daveshanley/vacuum@$version" "$suite_root/go.log" || {
	printf 'V1: installer did not build the pinned module\n' >&2
	exit 1
}
[[ -f "$cache_dir/vacuum" && -f "$cache_dir/vacuum.provenance" ]] || {
	printf 'V1: cache misses the binary or its provenance\n' >&2
	exit 1
}
provenance_digest_matches || {
	printf 'V1: provenance digest does not match the cached binary\n' >&2
	exit 1
}
[[ "$("$suite_root/install-v1/vacuum")" == "$version" ]] || {
	printf 'V1: installed vacuum does not report %s\n' "$version" >&2
	exit 1
}

# V2 warm: a verified entry restores with no download and no build.
: >"$suite_root/go.log"
output="$(VACUUM_CACHE_DIR="$cache_dir" FAKE_NO_BUILD=1 \
	run_installer "$good_sum" "$version" "$suite_root/install-v2" 2>&1)" || {
	printf 'V2: warm install failed: %s\n' "$output" >&2
	exit 1
}
[[ "$output" == *'restored verified vacuum'* ]] || {
	printf 'V2: missing restore message: %s\n' "$output" >&2
	exit 1
}
if grep -q 'mod download' "$suite_root/go.log" || grep -q '^install ' "$suite_root/go.log"; then
	printf 'V2: warm install downloaded or built the module\n' >&2
	exit 1
fi
[[ "$("$suite_root/install-v2/vacuum")" == "$version" ]] || {
	printf 'V2: restored vacuum does not report %s\n' "$version" >&2
	exit 1
}

# V3 corrupt binary: the digest check rejects the entry and the rebuild refills it.
printf 'x' >>"$cache_dir/vacuum"
v3_status=0
output="$(VACUUM_CACHE_DIR="$cache_dir" FAKE_NO_BUILD=1 \
	run_installer "$good_sum" "$version" "$suite_root/install-v3" 2>&1)" || v3_status=$?
if ((v3_status == 0)) || [[ "$output" != *'build not allowed'* ]] || [[ -e "$suite_root/install-v3/vacuum" ]]; then
	printf 'V3: want a refused rebuild with no binary left, got status %d: %s\n' "$v3_status" "$output" >&2
	exit 1
fi
[[ "$output" == *'binary digest does not match its provenance'* ]] || {
	printf 'V3: missing digest rejection: %s\n' "$output" >&2
	exit 1
}
: >"$suite_root/go.log"
VACUUM_CACHE_DIR="$cache_dir" run_installer "$good_sum" "$version" "$suite_root/install-v3b" >/dev/null || {
	printf 'V3: rebuild after the rejected entry failed\n' >&2
	exit 1
}
grep -Fxq "install github.com/daveshanley/vacuum@$version" "$suite_root/go.log" || {
	printf 'V3: rebuild did not build the pinned module\n' >&2
	exit 1
}
provenance_digest_matches || {
	printf 'V3: rebuilt provenance digest does not match the cached binary\n' >&2
	exit 1
}

# V4 provenance mismatch: another Go version rejects the entry, and the rebuild records it.
FAKE_GOVERSION=go1.27.5 VACUUM_CACHE_DIR="$cache_dir" FAKE_NO_BUILD=1 \
	expect_failure 'provenance does not match the pinned inputs' "$suite_root/install-v4" \
	run_installer "$good_sum" "$version" "$suite_root/install-v4" || {
	printf 'V4: Go version drift was not rejected\n' >&2
	exit 1
}
FAKE_GOVERSION=go1.27.5 VACUUM_CACHE_DIR="$cache_dir" \
	run_installer "$good_sum" "$version" "$suite_root/install-v4b" >/dev/null || {
	printf 'V4: rebuild with the new Go version failed\n' >&2
	exit 1
}
grep -Fxq 'go_goversion=go1.27.5' "$cache_dir/vacuum.provenance" || {
	printf 'V4: provenance does not record go1.27.5\n' >&2
	exit 1
}

# V5 build info mismatch: provenance and digest agree, the embedded module sum does not.
sed "s|^# buildinfo .*|# buildinfo $version $other_sum|" "$cache_dir/vacuum" >"$suite_root/vacuum.edit"
install -m 0755 "$suite_root/vacuum.edit" "$cache_dir/vacuum"
digest="$(sha256sum "$cache_dir/vacuum")"
sed '$d' "$cache_dir/vacuum.provenance" >"$suite_root/provenance.edit"
printf 'sha256=%s\n' "${digest%% *}" >>"$suite_root/provenance.edit"
install -m 0644 "$suite_root/provenance.edit" "$cache_dir/vacuum.provenance"
FAKE_GOVERSION=go1.27.5 VACUUM_CACHE_DIR="$cache_dir" FAKE_NO_BUILD=1 \
	expect_failure 'build info does not name the pinned module sum' "$suite_root/install-v5" \
	run_installer "$good_sum" "$version" "$suite_root/install-v5" || {
	printf 'V5: foreign build info was not rejected\n' >&2
	exit 1
}

# V6 symlink entry: a link to a valid copy is still refused.
VACUUM_CACHE_DIR="$cache_dir" run_installer "$good_sum" "$version" "$suite_root/install-v6-fill" >/dev/null
install -m 0755 "$cache_dir/vacuum" "$suite_root/vacuum-valid"
rm -f "$cache_dir/vacuum"
ln -s "$suite_root/vacuum-valid" "$cache_dir/vacuum"
VACUUM_CACHE_DIR="$cache_dir" FAKE_NO_BUILD=1 \
	expect_failure 'entry is not a regular file' "$suite_root/install-v6" \
	run_installer "$good_sum" "$version" "$suite_root/install-v6" || {
	printf 'V6: symlinked entry was not rejected\n' >&2
	exit 1
}

# V7 cache key: stable, and changed by the Go version and by the module sum pin.
write_pins "$version" "$good_sum"
key_a="$(cache_key)"
key_b="$(cache_key)"
[[ "$key_a" =~ ^vacuum-linux-amd64-[0-9a-f]{32}$ ]] || {
	printf 'V7: cache key has the wrong shape: %s\n' "$key_a" >&2
	exit 1
}
[[ "$key_a" == "$key_b" ]] || {
	printf 'V7: cache key differs between identical runs\n' >&2
	exit 1
}
[[ "$(FAKE_GOVERSION=go1.27.5 cache_key)" != "$key_a" ]] || {
	printf 'V7: Go version did not change the cache key\n' >&2
	exit 1
}
write_pins "$version" "$other_sum"
[[ "$(cache_key)" != "$key_a" ]] || {
	printf 'V7: module sum pin did not change the cache key\n' >&2
	exit 1
}
write_pins "$version" 'sha256:not-a-sum'
expect_failure 'invalid pinned module sum' "$suite_root/key-pin" cache_key || {
	printf 'V7: malformed sum pin produced a cache key\n' >&2
	exit 1
}
write_pins '0.33.1' "$good_sum"
expect_failure 'invalid VACUUM_VERSION' "$suite_root/key-semver" cache_key || {
	printf 'V7: version pin without the v prefix produced a cache key\n' >&2
	exit 1
}

# V8 input record: provenance without digest matches the pinned inputs and fake go env.
write_pins "$version" "$good_sum"
VACUUM_CACHE_DIR="$cache_dir" run_installer "$good_sum" "$version" "$suite_root/install-v8" >/dev/null
printf '%s\n' \
	'module=github.com/daveshanley/vacuum' \
	"version=${version}" \
	"module_sum=${good_sum}" \
	'go_goversion=go1.27.4' \
	'go_goos=linux' \
	'go_goarch=amd64' \
	'go_goamd64=' \
	'go_goarm64=' \
	'go_cgo_enabled=' \
	'go_goflags=' \
	'go_goexperiment=' \
	>"$suite_root/v8-expected.txt"
sed '$d' "$cache_dir/vacuum.provenance" >"$suite_root/v8-actual.txt"
if ! diff -u "$suite_root/v8-expected.txt" "$suite_root/v8-actual.txt" >&2; then
	printf 'V8: input record changed\n' >&2
	exit 1
fi

printf 'vacuum installer policy tests passed\n'
