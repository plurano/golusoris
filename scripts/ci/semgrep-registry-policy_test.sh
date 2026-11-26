#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly repo_root
# shellcheck source=scripts/ci/lib/policy-test.sh
. "$repo_root/scripts/ci/lib/policy-test.sh"
policy_suite_init golusoris-semgrep-registry

# Fixtures cannot inherit hook repository variables or user Git configuration.
for git_var in "${!GIT_@}"; do unset "$git_var"; done
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1

# Separate version variable keeps tool-versions_test.sh literal grep off this test.
readonly fake_version='9.9.9'
readonly fake_digest='sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
readonly fake_image="semgrep/semgrep:${fake_version}@${fake_digest}"
# Split literal prevents git grep in S1 from matching this test file itself.
readonly sec_audit_ruleset='p/security-''audit'

fail_case() {
	local case_id="$1"
	shift
	printf '%s: %s\n' "$case_id" "$*" >&2
	exit 1
}

stream_listing() {
	if [[ -f "$FAKE_DOCKER_TAR" ]]; then
		cat -- "$FAKE_DOCKER_TAR"
	else
		printf '<no stream recorded>\n'
	fi
}

write_fake_docker() {
	local path="$1"
	cat >"$path" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
: >"$FAKE_DOCKER_CALLED"
case "${1:-}" in
	info)
		exit "${FAKE_DOCKER_INFO_STATUS:-0}"
		;;
	run)
		printf '%s\n' "$@" >"$FAKE_DOCKER_ARGS"
		tar -tf - >"$FAKE_DOCKER_TAR"
		exit "${FAKE_DOCKER_STATUS:-0}"
		;;
	*)
		printf 'unexpected fake docker command: %s\n' "${1:-}" >&2
		exit 97
		;;
esac
SH
	chmod +x "$path"
}

reset_fake_docker() {
	rm -f -- "$FAKE_DOCKER_CALLED" "$FAKE_DOCKER_ARGS" "$FAKE_DOCKER_TAR"
}

new_fixture() {
	local name="$1"
	local root="$suite_root/$name"
	mkdir -p "$root/scripts/ci" "$root/scripts/hooks" "$root/tools" "$root/sub" \
		"$root/.config/hiss/testdata"
	cp "$repo_root/scripts/ci/semgrep-registry.sh" "$root/scripts/ci/semgrep-registry.sh"
	cp "$repo_root/scripts/hooks/semgrep.sh" "$root/scripts/hooks/semgrep.sh"
	chmod +x "$root/scripts/ci/semgrep-registry.sh" "$root/scripts/hooks/semgrep.sh"
	printf 'SEMGREP_VERSION=9.9.9\nSEMGREP_IMAGE_DIGEST=%s\n' "$fake_digest" \
		>"$root/tools/tool-versions.env"
	printf 'x = 1\n' >"$root/a.py"
	printf 'package sub\n' >"$root/sub/b.go"
	printf 'package testdata\n' >"$root/.config/hiss/testdata/x.go"
	git -C "$root" init -q
	git -C "$root" add .
	git -C "$root" -c user.name=policy -c user.email=policy@example.invalid -c commit.gpgsign=false \
		commit -q -m fixture
	printf '%s\n' "$root"
}

has_arg() {
	local want="$1"
	shift
	local arg idx=0
	for arg in "$@"; do
		((idx += 1))
		if (( idx > 256 )); then
			break
		fi
		if [[ "$arg" == "$want" ]]; then
			return 0
		fi
	done
	return 1
}

has_arg_pair() {
	local flag="$1" value="$2"
	shift 2
	local idx=0
	while (( $# >= 2 )); do
		((idx += 1))
		if (( idx > 256 )); then
			break
		fi
		if [[ "$1" == "$flag" && "$2" == "$value" ]]; then
			return 0
		fi
		shift
	done
	return 1
}

assert_r1_flags() {
	(( $# > 0 )) || fail_case R1 'no docker arguments recorded'
	[[ "${!#}" == '.' ]] || fail_case R1 "last argument is not . (got ${!#})"
	local item idx=0
	for item in "$fake_image" --error --metrics=off --quiet; do
		((idx += 1))
		if (( idx > 8 )); then
			break
		fi
		has_arg "$item" "$@" || fail_case R1 "missing argument $item"
	done
}

assert_r1_pairs() {
	local item idx=0
	for item in p/golang p/javascript "$sec_audit_ruleset"; do
		((idx += 1))
		if (( idx > 8 )); then
			break
		fi
		has_arg_pair --config "$item" "$@" || fail_case R1 "missing --config $item"
	done
	idx=0
	for item in .config/hiss/testdata .config/hiss/semgrep/testdata; do
		((idx += 1))
		if (( idx > 8 )); then
			break
		fi
		has_arg_pair --exclude "$item" "$@" || fail_case R1 "missing --exclude $item"
	done
}

test_r1() {
	local root="$1"
	reset_fake_docker
	local rc=0
	bash "$root/scripts/ci/semgrep-registry.sh" --files a.py sub/b.go || rc=$?
	(( rc == 0 )) || fail_case R1 "expected exit 0, got $rc"
	[[ -f "$FAKE_DOCKER_ARGS" ]] || fail_case R1 'docker run arguments not recorded'
	local -a args=()
	local line idx=0
	while IFS= read -r line || [[ -n "$line" ]]; do
		((idx += 1))
		if (( idx > 256 )); then
			break
		fi
		args+=("$line")
	done <"$FAKE_DOCKER_ARGS"
	assert_r1_flags "${args[@]}"
	assert_r1_pairs "${args[@]}"
	[[ -f "$FAKE_DOCKER_TAR" && "$(cat "$FAKE_DOCKER_TAR")" == $'a.py\nsub/b.go' ]] || \
		fail_case R1 "unexpected tar stream: $(stream_listing)"
}

assert_scan_exit() {
	local case_id="$1" root="$2" want_rc="$3"
	reset_fake_docker
	local err='' rc=0
	err="$(FAKE_DOCKER_STATUS="$want_rc" bash "$root/scripts/ci/semgrep-registry.sh" --files a.py 2>&1 >/dev/null)" || rc=$?
	(( rc == want_rc )) || fail_case "$case_id" "expected exit $want_rc, got $rc ($err)"
	[[ "$err" == *"registry scan failed with exit status $want_rc"* ]] || \
		fail_case "$case_id" "missing expected stderr message in: $err"
}

test_r4() {
	local root
	root="$(new_fixture r4)"
	printf 'ignored.py\n' >"$root/.gitignore"
	printf 'tracked = 1\n' >"$root/tracked.py"
	git -C "$root" add .gitignore tracked.py
	git -C "$root" -c user.name=policy -c user.email=policy@example.invalid -c commit.gpgsign=false \
		commit -q -m 'add tracked and gitignore'
	printf 'untracked = 1\n' >"$root/untracked.py"
	printf 'ignored = 1\n' >"$root/ignored.py"
	reset_fake_docker
	local rc=0
	bash "$root/scripts/ci/semgrep-registry.sh" || rc=$?
	(( rc == 0 )) || fail_case R4 "expected exit 0, got $rc"
	grep -Fxq 'tracked.py' "$FAKE_DOCKER_TAR" || fail_case R4 'stream missing tracked.py'
	grep -Fxq 'untracked.py' "$FAKE_DOCKER_TAR" || fail_case R4 'stream missing untracked.py'
	if grep -Fxq 'ignored.py' "$FAKE_DOCKER_TAR"; then
		fail_case R4 'stream included ignored.py'
	fi
	if grep -Eq '^\.git(/|$)' "$FAKE_DOCKER_TAR"; then
		fail_case R4 'stream included .git entry'
	fi
}

assert_r5_usage() {
	local label="$1" root="$2"
	shift 2
	reset_fake_docker
	local err='' rc=0
	err="$(bash "$root/scripts/ci/semgrep-registry.sh" "$@" 2>&1 >/dev/null)" || rc=$?
	(( rc == 2 )) || fail_case R5 "$label: expected exit 2, got $rc ($err)"
	[[ "$err" == *'usage:'* ]] || fail_case R5 "$label: missing usage on stderr ($err)"
	[[ ! -e "$FAKE_DOCKER_CALLED" ]] || fail_case R5 "$label: docker must not run"
}

test_r5() {
	local root="$1"
	shift
	assert_r5_usage 'empty --files' "$root" --files
	assert_r5_usage 'unknown option' "$root" --unknown
	assert_r5_usage '4097 paths' "$root" --files "$@"
}

test_r6() {
	local root
	root="$(new_fixture r6)"
	printf 'x = 1\n' >"$root/-dash.py"
	reset_fake_docker
	local rc=0
	bash "$root/scripts/ci/semgrep-registry.sh" --files -dash.py || rc=$?
	(( rc == 0 )) || fail_case R6 "expected exit 0, got $rc"
	[[ -f "$FAKE_DOCKER_TAR" && "$(cat "$FAKE_DOCKER_TAR")" == '-dash.py' ]] || \
		fail_case R6 "expected -dash.py in stream, got: $(stream_listing)"
}

test_r7() {
	local root
	root="$(new_fixture r7)"
	printf 'kept = 1\n' >"$root/kept.py"
	printf 'removed = 1\n' >"$root/removed.py"
	git -C "$root" add kept.py removed.py
	git -C "$root" -c user.name=policy -c user.email=policy@example.invalid -c commit.gpgsign=false \
		commit -q -m 'add kept and removed'
	rm -- "$root/removed.py"
	reset_fake_docker
	local rc=0
	bash "$root/scripts/ci/semgrep-registry.sh" || rc=$?
	(( rc == 0 )) || fail_case R7 "expected exit 0, got $rc"
	[[ -f "$FAKE_DOCKER_TAR" ]] || fail_case R7 'tar stream not recorded'
	grep -Fxq 'kept.py' "$FAKE_DOCKER_TAR" || \
		fail_case R7 "stream missing kept.py: $(stream_listing)"
	if grep -Fxq 'removed.py' "$FAKE_DOCKER_TAR"; then
		fail_case R7 "stream included removed.py: $(stream_listing)"
	fi
}

test_h1() {
	local root="$1"
	reset_fake_docker
	local rc=0
	bash "$root/scripts/hooks/semgrep.sh" --pushed a.py || rc=$?
	(( rc == 0 )) || fail_case H1 "expected exit 0, got $rc"
	[[ -f "$FAKE_DOCKER_TAR" && "$(cat "$FAKE_DOCKER_TAR")" == 'a.py' ]] || \
		fail_case H1 'expected a.py in stream'
	reset_fake_docker
	rc=0
	FAKE_DOCKER_STATUS=1 bash "$root/scripts/hooks/semgrep.sh" --pushed a.py >/dev/null 2>&1 || rc=$?
	(( rc == 1 )) || fail_case H1 "expected exit 1 on failed scan, got $rc"
}

test_h2() {
	local root="$1"
	reset_fake_docker
	local out='' rc=0
	out="$(bash "$root/scripts/hooks/semgrep.sh" --pushed deleted.py .config/hiss/testdata/x.go 2>&1)" || rc=$?
	(( rc == 0 )) || fail_case H2 "expected exit 0, got $rc ($out)"
	[[ "$out" == *'semgrep: no pushed files to scan, skipping'* ]] || \
		fail_case H2 "missing skip line in: $out"
	[[ ! -e "$FAKE_DOCKER_ARGS" && ! -e "$FAKE_DOCKER_TAR" ]] || \
		fail_case H2 'docker run must not be called'
}

test_h3() {
	local root
	root="$(new_fixture h3)"
	printf '# uncommitted\n' >>"$root/sub/b.go"
	reset_fake_docker
	local out='' rc=0
	out="$(bash "$root/scripts/hooks/semgrep.sh" --pushed sub/b.go 2>&1)" || rc=$?
	(( rc == 1 )) || fail_case H3 "expected exit 1, got $rc ($out)"
	[[ "$out" == *'sub/b.go'* ]] || fail_case H3 "refusal did not name sub/b.go: $out"
	[[ ! -e "$FAKE_DOCKER_ARGS" && ! -e "$FAKE_DOCKER_TAR" ]] || \
		fail_case H3 'docker run must not be called'
}

assert_h4_skip() {
	local label="$1" docker_bin="$2" info_status="$3" root="$4"
	reset_fake_docker
	local err='' rc=0
	err="$(DOCKER_BIN="$docker_bin" FAKE_DOCKER_INFO_STATUS="$info_status" \
		bash "$root/scripts/hooks/semgrep.sh" --pushed a.py 2>&1 >/dev/null)" || rc=$?
	(( rc == 0 )) || fail_case H4 "$label: expected exit 0, got $rc ($err)"
	[[ "$err" == *'semgrep: docker is not available, skipping; CI security-scan Semgrep remains authoritative'* ]] || \
		fail_case H4 "$label: missing docker skip line on stderr ($err)"
	[[ ! -e "$FAKE_DOCKER_ARGS" && ! -e "$FAKE_DOCKER_TAR" ]] || \
		fail_case H4 "$label: docker run must not be called"
}

test_h4() {
	local root="$1" fake_docker="$2"
	assert_h4_skip 'missing DOCKER_BIN' "$suite_root/missing-docker" 0 "$root"
	assert_h4_skip 'docker info status 1' "$fake_docker" 1 "$root"
}

test_h5() {
	local root="$1"
	shift
	reset_fake_docker
	local rc=0
	bash "$root/scripts/hooks/semgrep.sh" --pushed 'sub\b.go' || rc=$?
	(( rc == 0 )) || fail_case H5 "expected exit 0 on backslash path, got $rc"
	[[ -f "$FAKE_DOCKER_TAR" && "$(cat "$FAKE_DOCKER_TAR")" == 'sub/b.go' ]] || \
		fail_case H5 'expected sub/b.go in stream'
	reset_fake_docker
	rc=0
	bash "$root/scripts/hooks/semgrep.sh" --pushed "$@" >/dev/null 2>&1 || rc=$?
	(( rc == 2 )) || fail_case H5 "expected exit 2 on 4097 paths, got $rc"
	[[ ! -e "$FAKE_DOCKER_ARGS" ]] || fail_case H5 'docker run must not be called on 4097 paths'
}

test_s1() {
	local matches='' rc=0
	matches="$(git -C "$repo_root" grep --untracked -l -F "$sec_audit_ruleset" -- ':!*.md')" || rc=$?
	if (( rc != 0 )) || [[ "$matches" != 'scripts/ci/semgrep-registry.sh' ]]; then
		fail_case S1 "expected only scripts/ci/semgrep-registry.sh for $sec_audit_ruleset, got (rc=$rc): $matches"
	fi
	local step_block
	step_block="$(awk '
		$0 == "      - name: Run Semgrep" { active = 1; next }
		active && /^      - / { exit }
		active && NF > 0 { print }
	' "$repo_root/.github/workflows/security-scan.yml")"
	local want_step=$'        shell: bash\n        run: bash scripts/ci/semgrep-registry.sh'
	[[ "$step_block" == "$want_step" ]] || \
		fail_case S1 "unexpected Run Semgrep step in security-scan.yml: $step_block"
	grep -Fq 'run: bash scripts/hooks/semgrep.sh --pushed {push_files}' "$repo_root/lefthook.yml" || \
		fail_case S1 'lefthook.yml missing pre-push semgrep command'
}

main() {
	local fake_docker="$suite_root/fake-docker"
	write_fake_docker "$fake_docker"
	export DOCKER_BIN="$fake_docker"
	export FAKE_DOCKER_CALLED="$suite_root/docker.called"
	export FAKE_DOCKER_ARGS="$suite_root/docker.args"
	export FAKE_DOCKER_TAR="$suite_root/docker.tar"

	local root
	root="$(new_fixture base)"

	local -a over_paths=()
	local i
	for ((i = 1; i <= 4097; i += 1)); do
		over_paths+=("p${i}.py")
	done

	test_r1 "$root"
	assert_scan_exit R2 "$root" 1
	assert_scan_exit R3 "$root" 2
	test_r4
	test_r5 "$root" "${over_paths[@]}"
	test_r6
	test_r7
	test_h1 "$root"
	test_h2 "$root"
	test_h3
	test_h4 "$root" "$fake_docker"
	test_h5 "$root" "${over_paths[@]}"
	test_s1

	printf 'Registry Semgrep runner and pre-push hook policy tests passed\n'
}

main "$@"
