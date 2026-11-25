#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2

set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly TEST_DIR

export GOFLAGS='' GOWORK=off GOTOOLCHAIN=local
# Fixture git commands must ignore the caller's repository and user configuration.
for git_var in "${!GIT_@}"; do unset "$git_var"; done
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1

# shellcheck source=scripts/ci/fault-recovery.sh
source "$TEST_DIR/fault-recovery.sh"

scratch_dirs=()

# Removes registered artifacts in reverse order.
cleanup() {
	local index
	for ((index = ${#scratch_dirs[@]} - 1; index >= 0; index--)); do
		remove_scratch "${scratch_dirs[index]}"
	done
}
trap cleanup EXIT
suite_root="$(mktemp -d "${TMPDIR:-/tmp}/golusoris-fault-recovery-policy.XXXXXX")"
readonly suite_root
scratch_dirs+=("$suite_root")

# Module whose tests recover a nil-dereference fault and an ordinary panic.
make_fixture() {
	local dir="$1"
	mkdir -p "$dir"
	printf 'module example.com/fixture\n\ngo 1.24\n' >"$dir/go.mod"
	cat >"$dir/fixture_test.go" <<'GO'
package fixture

import "testing"

type node struct{ next *node }

func recovered(f func()) (ok bool) {
	defer func() { ok = recover() != nil }()
	f()
	return false
}

func TestFault(t *testing.T) {
	var n *node
	if !recovered(func() { _ = n.next }) {
		t.Fatal("nil dereference did not panic")
	}
}

func TestPlainPanic(t *testing.T) {
	if !recovered(func() { panic("plain") }) {
		t.Fatal("panic was not recovered")
	}
}
GO
}

# Runs the fixture tests named by $2 on the marked GOROOT $1 and prints the report.
scan_fixture() {
	local goroot="$1" run="$2"
	(cd "$suite_root/fixture" &&
		GOROOT="$goroot" PATH="$goroot/bin:$PATH" go test -count=1 -json -run "$run" .) |
		report_faults
}

# Reports a broken policy case and exits non-zero.
fail() {
	printf 'fault-recovery policy: %s\n' "$*" >&2
	exit 1
}

# Checks recovered faults and ordinary panics on the marked runtime.
check_fault_report() {
	make_fixture "$suite_root/fixture"
	mark_goroot "$suite_root/goroot"
	local report
	report="$(scan_fixture "$suite_root/goroot" '^TestFault$')"
	[[ "$report" == *"example.com/fixture (seen in TestFault)"* ]] ||
		fail "recovered fault not reported: ${report:-<empty>}"
	report="$(scan_fixture "$suite_root/goroot" '^TestPlainPanic$')"
	[[ -z "$report" ]] || fail "ordinary panic reported as fault: $report"
}

# Checks rejection of a runtime without the patched functions.
check_bare_runtime() {
	# A runtime without the patched functions must stop the gate, not pass it.
	printf 'package runtime\n' >"$suite_root/bare_panic.go"
	if mark_panic_go "$suite_root/bare_panic.go" 2>/dev/null; then
		fail "mark_panic_go accepted a runtime without panicmem"
	fi
}

# Checks copying a read-only GOROOT without changing its source.
check_readonly_goroot() {
	local ro_goroot="$suite_root/ro-goroot" mode
	local ro_panic="$ro_goroot/src/runtime/panic.go"
	mkdir -p "$ro_goroot/src/runtime"
	printf 'func panicmem() {\n}\nfunc panicmemAddr(addr uintptr) {\n}\n' >"$suite_root/ro-panic.go"
	cp "$suite_root/ro-panic.go" "$ro_panic"
	chmod 0444 "$ro_panic"
	chmod 0555 "$ro_goroot" "$ro_goroot/src" "$ro_goroot/src/runtime"
	if ! (
		# Returns only the planted read-only GOROOT for the copy test.
		go() {
			[[ "$#" -eq 2 && "$1" == env && "$2" == GOROOT ]] || return 1
			printf '%s\n' "$ro_goroot"
		}
		mark_goroot "$suite_root/ro-marked"
	); then
		fail "mark_goroot rejected a read-only GOROOT"
	fi
	grep -q "$MARKER" "$suite_root/ro-marked/src/runtime/panic.go" ||
		fail "read-only GOROOT copy missing recovered-fault marker"
	cmp -s "$suite_root/ro-panic.go" "$ro_panic" ||
		fail "mark_goroot changed source panic.go content"
	mode="$(stat -c %a "$ro_panic" 2>/dev/null || stat -f %Lp "$ro_panic")"
	[[ "$mode" == 444 ]] || fail "mark_goroot changed source panic.go mode: $mode"
}

# Checks default scratch bases with TMPDIR set and unset.
check_default_bases() {
	local scratch_tmp scratch_unset scratch physical_scratch system_tmp=/tmp physical_tmp
	physical_tmp="$(cd -P "$system_tmp" && pwd)"
	scratch_tmp="$(export TMPDIR=/tmp; make_scratch "${SCRATCH_BASES[@]}")" ||
		fail "make_scratch failed with TMPDIR=/tmp"
	scratch_dirs+=("$scratch_tmp")
	scratch_unset="$(unset TMPDIR; make_scratch "${SCRATCH_BASES[@]}")" ||
		fail "make_scratch failed with TMPDIR unset"
	scratch_dirs+=("$scratch_unset")
	for scratch in "$scratch_tmp" "$scratch_unset"; do
		[[ -d "$scratch" ]] || fail "make_scratch returned no directory: $scratch"
		physical_scratch="$(cd -P "$scratch" && pwd)"
		[[ "$physical_scratch" != "$physical_tmp" && "$physical_scratch" != "$physical_tmp/"* ]] ||
			fail "make_scratch used an unsafe directory: $scratch"
	done
}

# Checks fallback from a checkout under /tmp to /var/tmp.
check_tmp_checkout() {
	local scratch physical_scratch system_tmp=/tmp
	local checkout="$system_tmp/golusoris-checkout-$$" fallback
	[[ ! -e "$checkout" ]] || fail "checkout fixture already exists: $checkout"
	scratch_dirs+=("$checkout")
	scratch="$(make_scratch "$checkout/.workingdir/cache" /var/tmp)" ||
		fail "make_scratch failed to fall back from a checkout under /tmp"
	scratch_dirs+=("$scratch")
	[[ ! -e "$checkout/.workingdir/cache" ]] || fail "make_scratch created the skipped /tmp base"
	fallback="$(cd -P /var/tmp && pwd)"
	[[ -d "$scratch" ]] || fail "make_scratch fallback returned no directory: $scratch"
	physical_scratch="$(cd -P "$scratch" && pwd)"
	[[ "$physical_scratch" == "$fallback/"fault-recovery.?????? ]] ||
		fail "make_scratch did not use the /var/tmp fallback directly: $scratch"
}

# Checks rejection of a scratch base symlinked into /tmp.
check_symlink_base() {
	local scratch system_tmp=/tmp link_target
	local outside error_file="$suite_root/scratch-error" accepted=0
	outside="$(mktemp -d /var/tmp/golusoris-scratch-policy.XXXXXX)"
	scratch_dirs+=("$outside")
	link_target="$(mktemp -d "$system_tmp/golusoris-scratch-link.XXXXXX")"
	scratch_dirs+=("$link_target")
	ln -s "$link_target" "$outside/link"
	if scratch="$(make_scratch "$outside/link/cache" 2>"$error_file")"; then
		accepted=1
		scratch_dirs+=("$scratch")
	fi
	((accepted == 0)) || fail "make_scratch accepted a symlink into /tmp"
	[[ "$(cat "$error_file")" == "fault-recovery: no scratch directory outside /tmp: $outside/link/cache" ]] ||
		fail "make_scratch did not explain the rejected symlink base"
}

# Fails unless the fault-recovery job exports CI_SCOPE_BASE to all steps.
check_job_scope() {
	local workflow="$TEST_DIR/../../.github/workflows/ci.yml" header
	header="$(awk '/^  fault-recovery:$/ { inside = 1; next } inside && /^  [a-z]/ { exit } inside { print }' "$workflow")"
	header="${header%%    steps:*}"
	if ! grep -Fq '    env:' <<<"$header" ||
		! grep -Fq \
			"      CI_SCOPE_BASE: \${{ github.event_name == 'pull_request' && github.event.pull_request.base.sha || '' }}" \
			<<<"$header"; then
		fail "fault-recovery job does not pass CI_SCOPE_BASE to its steps"
	fi
}

# Writes a passing test so a scanned fixture module exits 0.
write_ok_test() {
	local path="$1" package_name="$2"
	printf 'package %s\n\nimport "testing"\n\nfunc TestOK(t *testing.T) {}\n' "$package_name" >"$path"
}

# Builds a four-module git fixture with ci scripts and a single ok test per module.
make_scope_fixture() {
	local dir="$1"
	mkdir -p "$dir/scripts/ci" "$dir/core" "$dir/app" "$dir/lone"
	cp "$TEST_DIR/fault-recovery.sh" "$TEST_DIR/go-modules.sh" \
		"$TEST_DIR/ci_scope.py" "$TEST_DIR/gomod.py" "$dir/scripts/ci/"
	printf '.workingdir/\n' >"$dir/.gitignore"
	git -C "$dir" init -q
	git -C "$dir" config user.name 'Test Runner'
	git -C "$dir" config user.email 'test@example.com'
	git -C "$dir" config commit.gpgsign false
	printf 'module example.com/root\n\ngo 1.24\n\nreplace example.com/core => ./core\n' >"$dir/go.mod"
	printf 'module example.com/core\n\ngo 1.24\n' >"$dir/core/go.mod"
	printf 'module example.com/app\n\ngo 1.24\n\nreplace example.com/core => ../core\n' >"$dir/app/go.mod"
	printf 'module example.com/lone\n\ngo 1.24\n' >"$dir/lone/go.mod"
	write_ok_test "$dir/ok_test.go" root
	write_ok_test "$dir/core/ok_test.go" core
	write_ok_test "$dir/app/ok_test.go" app
	write_ok_test "$dir/lone/ok_test.go" lone
	git -C "$dir" add -A
	git -C "$dir" -c commit.gpgsign=false commit -q -m 'init'
}

# Runs fault-recovery.sh scoped to base; stderr goes to the third argument.
scan_scope() {
	local dir="$1" base="$2" stderr_file="$3" status=0
	( cd "$dir" && CI_SCOPE_BASE="$base" bash scripts/ci/fault-recovery.sh ) >/dev/null 2>"$stderr_file" || status=$?
	return "$status"
}

# Prints module names from fault-recovery progress lines, sorted and space-joined.
scanned_modules() {
	local stderr_file="$1"
	grep -E '^fault-recovery: \[[0-9]+/[0-9]+\] ' "$stderr_file" 2>/dev/null |
		sed -E 's/^fault-recovery: \[[0-9]+\/[0-9]+\] //' | LC_ALL=C sort | paste -sd ' ' -
}

# Exercises scoped module selection for changed modules, global inputs, and failure paths.
check_scope_selection() {
	local scope_dir="$suite_root/scope" base head stderr_file="$suite_root/scope.stderr"
	local status modules
	mkdir -p "$scope_dir"
	make_scope_fixture "$scope_dir"
	base="$(git -C "$scope_dir" rev-parse HEAD)"

	printf '// touch\n' >>"$scope_dir/core/ok_test.go"
	git -C "$scope_dir" add -A
	git -C "$scope_dir" -c commit.gpgsign=false commit -q -m 'change core'
	status=0
	scan_scope "$scope_dir" "$base" "$stderr_file" || status=$?
	modules="$(scanned_modules "$stderr_file")"
	[[ "$status" == 0 && "$modules" == '. app core' ]] ||
		fail "S1: status=$status modules=${modules:-<empty>}"

	local scope_global="$suite_root/scope-global"
	make_scope_fixture "$scope_global"
	base="$(git -C "$scope_global" rev-parse HEAD)"
	printf 'linters: {}\n' >"$scope_global/.golangci.yml"
	git -C "$scope_global" add .golangci.yml
	git -C "$scope_global" -c commit.gpgsign=false commit -q -m 'global lint input'
	status=0
	scan_scope "$scope_global" "$base" "$stderr_file" || status=$?
	modules="$(scanned_modules "$stderr_file")"
	[[ "$status" == 0 && "$modules" == '. app core lone' ]] ||
		fail "S2: status=$status modules=${modules:-<empty>}"

	local scope_empty="$suite_root/scope-empty"
	make_scope_fixture "$scope_empty"
	head="$(git -C "$scope_empty" rev-parse HEAD)"
	status=0
	scan_scope "$scope_empty" "$head" "$stderr_file" || status=$?
	grep -q 'module selection is empty' "$stderr_file" || fail "S3: missing empty-selection error"
	if grep -qE '^fault-recovery: \[[0-9]+/[0-9]+\] ' "$stderr_file"; then
		fail "S3: progress lines present on empty selection"
	fi
	[[ "$status" != 0 ]] || fail "S3: status=$status expected nonzero"

	local scope_bad="$suite_root/scope-bad"
	make_scope_fixture "$scope_bad"
	status=0
	scan_scope "$scope_bad" '0000000000000000000000000000000000000000' "$stderr_file" || status=$?
	grep -q 'module discovery failed' "$stderr_file" || fail "S4: missing discovery-failure error"
	if grep -qE '^fault-recovery: \[[0-9]+/[0-9]+\] ' "$stderr_file"; then
		fail "S4: progress lines present on discovery failure"
	fi
	[[ "$status" != 0 ]] || fail "S4: status=$status expected nonzero"
}

# Checks EXIT trap cleanup with an apostrophe in the scratch path.
check_trap_quoting() {
	local quoted_dir="$suite_root/it's a dir"
	mkdir -p "$quoted_dir/read-only"
	printf 'fixture\n' >"$quoted_dir/read-only/file"
	chmod 0555 "$quoted_dir/read-only"
	(
		# shellcheck disable=SC2064 # WHY: expand the local scratch path when registering the trap.
		trap "$(scratch_trap "$quoted_dir")" EXIT
	) || fail "scratch_trap failed for an apostrophe in the path"
	[[ ! -e "$quoted_dir" ]] || fail "scratch_trap left its read-only directory behind"
}

# Runs the policy cases; exits non-zero through fail on the first broken one.
main() {
	check_fault_report
	check_bare_runtime
	check_readonly_goroot
	check_default_bases
	check_tmp_checkout
	check_symlink_base
	check_trap_quoting
	check_job_scope
	check_scope_selection
	printf 'fault-recovery policy: ok\n'
}

main "$@"
