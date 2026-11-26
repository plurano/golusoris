// SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
//
// SPDX-License-Identifier: EUPL-1.2

package twotier

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// TestL2CallDoesNotBlockL1Reads checks warm L1 hits while each L2 write path is stalled.
func TestL2CallDoesNotBlockL1Reads(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	cases := []struct {
		name    string
		op      string
		raw     string
		corrupt bool
		run     func(context.Context, *Typed[int]) error
	}{
		{name: "loader-fill-set", op: "set", raw: "1", run: fillL2LockTestKey},
		{name: "typed-set", op: "set", raw: "2", run: func(ctx context.Context, view *Typed[int]) error {
			return view.Set(ctx, "a", 2)
		}},
		{name: "typed-delete", op: "del", run: func(ctx context.Context, view *Typed[int]) error {
			return view.Delete(ctx, "a")
		}},
		{name: "invalidate-prefix", op: "del-prefix", run: func(ctx context.Context, view *Typed[int]) error {
			return view.InvalidatePrefix(ctx, "a")
		}},
		{name: "corrupt-entry-repair-del", op: "del", corrupt: true, run: fillL2LockTestKey},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(subtest *testing.T) {
			subtest.Parallel()
			ctx := subtest.Context()
			stub := newStubL2()
			view := NewTyped[int](newTestTwoTier(subtest, stub), "n")
			requireL2LockSetupSet(subtest, func() error { return view.Set(ctx, "b", 7) })
			if tc.corrupt {
				stub.data["n:a"] = []byte("not-json")
			}
			entered, release, hook := blockL2Write(subtest, tc.op, tc.raw)
			stub.beforeWrite = hook
			write := startL2LockOperation(subtest, release, func() error { return tc.run(ctx, view) })
			waitL2LockSignal(subtest, entered, "L2 write did not enter the stub")
			gets := stub.getCalls.Load()
			read := startL2LockOperation(subtest, release, func() error { return readCachedL2LockTestKey(ctx, view, "b", 7) })
			read.requireDone(subtest, "warm L1 read stalled behind blocked L2 call")
			require.Equal(subtest, gets, stub.getCalls.Load(), "warm read reached L2")
			release()
			write.requireDone(subtest, "L2 operation did not finish after release")
		})
	}
}

// TestLoaderFillOrderedBeforeSameKeySet protects explicit writes against older fills.
func TestLoaderFillOrderedBeforeSameKeySet(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	ctx := t.Context()
	stub := newStubL2()
	view := NewTyped[int](newTestTwoTier(t, stub), "n")
	entered, release, hook := blockL2Write(t, "set", "1")
	stub.beforeWrite = hook
	fill := startL2LockOperation(t, release, func() error { return fillL2LockTestKey(ctx, view) })
	waitL2LockSignal(t, entered, "loader fill did not enter L2")
	sets := stub.setCalls.Load()
	set := startL2LockOperation(t, release, func() error { return view.Set(ctx, "a", 2) })
	set.requireBlocked(t, "sync.Mutex.Lock", ").lockL2Key(")
	require.Equal(t, sets, stub.setCalls.Load(), "same-key Set reached L2 before the loader fill was released")
	release()
	fill.requireDone(t, "loader fill did not finish after release")
	set.requireDone(t, "same-key Set did not finish after release")
	value, ok, err := stub.Get(ctx, "n:a")
	require.NoError(t, err)
	require.True(t, ok)
	require.Equal(t, "2", string(value))
	gets := stub.getCalls.Load()
	read := startL2LockOperation(t, nil, func() error { return readCachedL2LockTestKey(ctx, view, "a", 2) })
	read.requireDone(t, "read after same-key Set did not finish")
	require.Equal(t, gets, stub.getCalls.Load(), "warm read reached L2")
}

// TestPrefixInvalidationWaitsForInFlightFill protects prefix deletes against older fills.
func TestPrefixInvalidationWaitsForInFlightFill(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	ctx := t.Context()
	stub := newStubL2()
	view := NewTyped[int](newTestTwoTier(t, stub), "n")
	entered, release, hook := blockL2Write(t, "set", "1")
	stub.beforeWrite = hook
	fill := startL2LockOperation(t, release, func() error { return fillL2LockTestKey(ctx, view) })
	waitL2LockSignal(t, entered, "loader fill did not enter L2")
	deletes := stub.delPrefixCalls.Load()
	invalidate := startL2LockOperation(t, release, func() error { return view.InvalidatePrefix(ctx, "") })
	invalidate.requireBlocked(t, "sync.RWMutex.Lock", ").invalidatePrefixBoth(")
	require.Equal(t, deletes, stub.delPrefixCalls.Load(), "prefix invalidation reached L2 before the loader fill was released")
	release()
	fill.requireDone(t, "loader fill did not finish after release")
	invalidate.requireDone(t, "prefix invalidation did not finish after release")
	require.Empty(t, stub.data)
	loaded := false
	read := startL2LockOperation(t, nil, func() error {
		value, err := view.Get(ctx, "a", func(context.Context) (int, error) {
			loaded = true
			return 2, nil
		})
		return checkL2LockTestValue(value, err, 2)
	})
	read.requireDone(t, "read after prefix invalidation did not finish")
	require.True(t, loaded)
}

// TestInFlightSetBlocksStaleL1Install protects reads from an older L2 result while Set commits.
func TestInFlightSetBlocksStaleL1Install(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	ctx := t.Context()
	stub := newStubL2()
	stub.data["n:a"] = []byte("1")
	tiers := newTestTwoTier(t, stub)
	view := NewTyped[int](tiers, "n")
	epoch, _, ok := view.l1Snapshot("n:a")
	require.False(t, ok)

	entered, release, hook := blockL2Write(t, "set", "2")
	stub.afterWrite = hook
	set := startL2LockOperation(t, release, func() error { return view.Set(ctx, "a", 2) })
	waitL2LockSignal(t, entered, "Set did not write to L2")
	first := startL2LockOperation(t, release, func() error { return readCachedL2LockTestKey(ctx, view, "a", 2) })
	first.requireDone(t, "read during Set did not finish")
	requireStaleL1InstallDone(t, release, view, epoch)
	second := startL2LockOperation(t, release, func() error { return readCachedL2LockTestKey(ctx, view, "a", 2) })
	second.requireDone(t, "read after stale install attempt did not finish")

	release()
	set.requireDone(t, "Set did not finish after release")
	gets := stub.getCalls.Load()
	last := startL2LockOperation(t, nil, func() error { return readCachedL2LockTestKey(ctx, view, "a", 2) })
	last.requireDone(t, "read after Set did not finish")
	require.Equal(t, gets, stub.getCalls.Load(), "read after Set reached L2")
}

// TestInFlightDeleteBlocksStaleL1Install protects a peer's newer value during a local Delete.
func TestInFlightDeleteBlocksStaleL1Install(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	ctx := t.Context()
	stub := newStubL2()
	stub.data["n:a"] = []byte("1")
	tiers := newTestTwoTier(t, stub)
	view := NewTyped[int](tiers, "n")
	epoch, _, ok := view.l1Snapshot("n:a")
	require.False(t, ok)

	entered, release, hook := blockL2Write(t, "del", "")
	stub.afterWrite = hook
	deleted := startL2LockOperation(t, release, func() error { return view.Delete(ctx, "a") })
	waitL2LockSignal(t, entered, "Delete did not remove the L2 value")
	writePeerL2Value(t, stub, "n:a", "2")

	first := startL2LockOperation(t, release, func() error { return readCachedL2LockTestKey(ctx, view, "a", 2) })
	first.requireDone(t, "read during Delete did not finish")
	requireStaleL1InstallDone(t, release, view, epoch)
	second := startL2LockOperation(t, release, func() error { return readCachedL2LockTestKey(ctx, view, "a", 2) })
	second.requireDone(t, "read after stale install attempt did not finish")

	release()
	deleted.requireDone(t, "Delete did not finish after release")
	last := startL2LockOperation(t, nil, func() error { return readCachedL2LockTestKey(ctx, view, "a", 2) })
	last.requireDone(t, "read after Delete did not finish")
}

// TestInFlightPrefixDeleteBlocksStaleL1Install verifies that only matching keys are fenced.
func TestInFlightPrefixDeleteBlocksStaleL1Install(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	cases := []struct {
		name     string
		prefix   string
		want     int
		wantGets int32
	}{
		{name: "covering-prefix", prefix: "a", want: 2, wantGets: 1},
		{name: "non-covering-prefix", prefix: "b", want: 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(subtest *testing.T) {
			subtest.Parallel()
			ctx := subtest.Context()
			stub := newStubL2()
			stub.data["n:a"] = []byte("1")
			view := NewTyped[int](newTestTwoTier(subtest, stub), "n")
			epoch, _, ok := view.l1Snapshot("n:a")
			require.False(subtest, ok)

			entered, release, hook := blockL2WriteForKey(subtest, "del-prefix", view.key(tc.prefix), "")
			stub.afterWrite = hook
			deleted := startL2LockOperation(subtest, release, func() error { return view.InvalidatePrefix(ctx, tc.prefix) })
			waitL2LockSignal(subtest, entered, "prefix delete did not finish its L2 write")
			writePeerL2Value(subtest, stub, "n:a", "2")

			first := startL2LockOperation(subtest, release, func() error { return readCachedL2LockTestKey(ctx, view, "a", 2) })
			first.requireDone(subtest, "read during prefix delete did not finish")
			requireStaleL1InstallDone(subtest, release, view, epoch)
			gets := stub.getCalls.Load()
			second := startL2LockOperation(subtest, release, func() error {
				return readCachedL2LockTestKey(ctx, view, "a", tc.want)
			})
			second.requireDone(subtest, "read after stale install attempt did not finish")
			require.Equal(subtest, tc.wantGets, stub.getCalls.Load()-gets, "L2 reads after stale install attempt")

			release()
			deleted.requireDone(subtest, "prefix delete did not finish after release")
			last := startL2LockOperation(subtest, nil, func() error { return readCachedL2LockTestKey(ctx, view, "a", tc.want) })
			last.requireDone(subtest, "read after prefix delete did not finish")
		})
	}
}

// TestRefusedEmptyPrefixKeepsCommittedL1 guards a committed value against a stale fill during refusal.
func TestRefusedEmptyPrefixKeepsCommittedL1(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	ctx := t.Context()
	stub := newStubL2()
	stub.data["n:a"] = []byte("1")
	tiers := newTestTwoTier(t, stub)
	view := NewTyped[int](tiers, "n")
	epoch0, _, ok := view.l1Snapshot("n:a")
	require.False(t, ok)
	requireL2LockSetupSet(t, func() error { return view.Set(ctx, "a", 2) })
	installRequested := make(chan struct{})
	requestInstall := sync.OnceFunc(func() { close(installRequested) })
	install := startL2LockOperation(t, nil, func() error {
		select {
		case <-installRequested:
			view.setL1IfCurrent("n:a", 1, epoch0)
		case <-ctx.Done():
		}
		return nil
	})
	hookFinished := make(chan bool, 1)
	stub.beforeWrite = func(op, key string, _ []byte) {
		if op == "del-prefix" && key == "" {
			select {
			case hookFinished <- tryStaleL1Install(requestInstall, install.done):
			default:
			}
		}
	}

	op := startL2LockOperation(t, nil, func() error { return tiers.InvalidatePrefix(ctx, "") })
	// Give the hook its own timeout before timing out the invalidation.
	require.True(t, l2LockSignalReceived(op.done, 2*l2LockTimeout), "cache/twotier: empty prefix invalidation did not finish")
	select {
	case finished := <-hookFinished:
		require.True(t, finished, "cache/twotier: stale L1 install attempt did not finish in the DelPrefix hook")
		require.NoError(t, install.err)
		t.Fatal("cache/twotier: empty prefix reached DelPrefix")
	default:
	}
	require.ErrorIs(t, op.err, errEmptyPrefix)
	require.EqualError(t, op.err, `cache/twotier: L2 invalidate prefix "": cache/twotier: refusing to scan-delete an empty prefix`)
	require.Zero(t, stub.delPrefixCalls.Load())

	requestInstall()
	install.requireDone(t, "stale L1 install attempt did not finish")
	gets := stub.getCalls.Load()
	read := startL2LockOperation(t, nil, func() error { return readCachedL2LockTestKey(ctx, view, "a", 2) })
	read.requireDone(t, "read after prefix refusal did not finish")
	require.Equal(t, gets, stub.getCalls.Load(), "read after prefix refusal reached L2")
}

// tryStaleL1Install lets the stub hook time out and return even if its caller holds mutation.
func tryStaleL1Install(request func(), done <-chan struct{}) bool {
	request()
	return l2LockSignalReceived(done, l2LockTimeout)
}

// TestFencedFillEvictsOlderL1 prevents reads from going back to an older warm value.
func TestFencedFillEvictsOlderL1(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	set := func(ctx context.Context, view *Typed[int]) error { return view.Set(ctx, "a", 2) }
	cases := [...]struct {
		name      string
		op        string
		raw       string
		peerWrite bool
		moveEpoch bool
		run       func(context.Context, *Typed[int]) error
	}{
		{name: "set", op: "set", raw: "2", run: set},
		{name: "del", op: "del", peerWrite: true, run: func(ctx context.Context, view *Typed[int]) error {
			return view.Delete(ctx, "a")
		}},
		{name: "del-prefix", op: "del-prefix", peerWrite: true, run: func(ctx context.Context, view *Typed[int]) error {
			return view.InvalidatePrefix(ctx, "a")
		}},
		{name: "set-epoch-moved", op: "set", raw: "2", moveEpoch: true, run: set},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(subtest *testing.T) {
			subtest.Parallel()
			ctx := subtest.Context()
			stub := newStubL2()
			stub.data["n:a"] = []byte("1")
			stub.data["n:c"] = []byte("7")
			tiers := newTestTwoTier(subtest, stub)
			view := NewTyped[int](tiers, "n")
			epoch := warmFencedFillKeys(subtest, view)

			entered, release, hook := blockL2Write(subtest, tc.op, tc.raw)
			stub.afterWrite = hook
			write := startL2LockOperation(subtest, release, func() error { return tc.run(ctx, view) })
			waitL2LockSignal(subtest, entered, "write did not reach L2")
			if tc.peerWrite {
				writePeerL2Value(subtest, stub, "n:a", "2")
			}
			if tc.moveEpoch {
				moveFencedFillEpoch(subtest, release, view, epoch)
			}

			install := startL2LockOperation(subtest, release, func() error {
				view.setL1IfCurrent("n:a", 2, epoch)
				return nil
			})
			install.requireDone(subtest, "fenced fill did not finish")
			checkFencedFillReads(subtest, release, view, stub)

			release()
			write.requireDone(subtest, "write did not finish after release")
			last := startL2LockOperation(subtest, nil, func() error { return readCachedL2LockTestKey(ctx, view, "a", 2) })
			last.requireDone(subtest, "read after write did not finish")
		})
	}
}

// TestFencedFillEvictsNewerCommittedL1 pins review r10 F1, where a fenced fill evicts a newer commit and forces a reload without installing the old value.
func TestFencedFillEvictsNewerCommittedL1(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	ctx := t.Context()
	stub := newStubL2()
	stub.data["n:a"] = []byte("1")
	stub.data["n:c"] = []byte("7")
	view := NewTyped[int](newTestTwoTier(t, stub), "n")
	missEpoch := warmFencedFillKeys(t, view)

	requireL2LockSetupSet(t, func() error { return view.Set(ctx, "a", 2) })
	committedEpoch, committed, present := snapshotL2LockTestKey(t, nil, view, "n:a")
	require.True(t, present)
	require.Equal(t, 2, committed)
	require.NotEqual(t, missEpoch, committedEpoch)
	expire := startL2LockOperation(t, nil, func() error {
		stub.mu.Lock()
		defer stub.mu.Unlock()
		delete(stub.data, "n:a")
		return nil
	})
	expire.requireDone(t, "L2 expiry did not finish")

	entered, release, hook := blockL2Write(t, "set", "3")
	stub.beforeWrite = hook
	set := startL2LockOperation(t, release, func() error { return view.Set(ctx, "a", 3) })
	waitL2LockSignal(t, entered, "later Set did not enter L2")
	requireStaleL1InstallDone(t, release, view, missEpoch)
	_, cached, kept := snapshotL2LockTestKey(t, release, view, "n:a")
	require.False(t, kept, "fenced fill must evict a, found %d in L1", cached)
	_, other, otherPresent := snapshotL2LockTestKey(t, release, view, "n:c")
	require.True(t, otherPresent, "unrelated key must stay warm in L1")
	require.Equal(t, 7, other)

	gets := stub.getCalls.Load()
	var loads atomic.Int32
	loaded := make(chan struct{}, 1)
	reload := startL2LockOperation(t, release, func() error {
		value, err := view.Get(ctx, "a", func(context.Context) (int, error) {
			if loads.Add(1) == 1 {
				loaded <- struct{}{}
			}
			return 1, nil
		})
		return checkL2LockTestValue(value, err, 1)
	})
	waitL2LockSignal(t, loaded, "read after eviction did not reach the loader")
	require.Equal(t, gets+1, stub.getCalls.Load())
	require.EqualValues(t, 1, loads.Load())

	release()
	set.requireDone(t, "later Set did not finish after release")
	reload.requireDone(t, "read after eviction did not finish after release")
	require.Equal(t, gets+1, stub.getCalls.Load())
	require.EqualValues(t, 1, loads.Load())
	checkFencedReloadCommit(t, view, stub)
}

// TestFencedFillEvictionOutlivesFailedWrite covers the review case where a fenced fill evicts a newer L1 commit during a write that later fails.
func TestFencedFillEvictionOutlivesFailedWrite(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	cases := [...]struct {
		name string
		op   string
		raw  string
		run  func(context.Context, *Typed[int]) error
	}{
		{name: "set", op: "set", raw: "3", run: func(ctx context.Context, view *Typed[int]) error {
			return view.Set(ctx, "a", 3)
		}},
		{name: "delete", op: "del", run: func(ctx context.Context, view *Typed[int]) error {
			return view.Delete(ctx, "a")
		}},
		{name: "prefix", op: "del-prefix", run: func(ctx context.Context, view *Typed[int]) error {
			return view.InvalidatePrefix(ctx, "a")
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(subtest *testing.T) {
			subtest.Parallel()
			ctx := subtest.Context()
			stub := newStubL2()
			stub.data["n:a"] = []byte("1")
			stub.data["n:c"] = []byte("7")
			view := NewTyped[int](newTestTwoTier(subtest, stub), "n")
			missEpoch := warmFencedFillKeys(subtest, view)

			requireL2LockSetupSet(subtest, func() error { return view.Set(ctx, "a", 2) })
			committedEpoch, committed, present := snapshotL2LockTestKey(subtest, nil, view, "n:a")
			require.True(subtest, present)
			require.Equal(subtest, 2, committed)
			require.NotEqual(subtest, missEpoch, committedEpoch)

			entered, release, hook := blockL2Write(subtest, tc.op, tc.raw)
			stub.beforeWrite = hook
			stub.failAfterHook = true
			write := startL2LockOperation(subtest, release, func() error { return tc.run(ctx, view) })
			waitL2LockSignal(subtest, entered, "later write did not enter L2")
			requireStaleL1InstallDone(subtest, release, view, missEpoch)
			requireFencedFillEviction(subtest, release, view)

			release()
			waitL2LockSignal(subtest, write.done, "later write did not finish after release")
			require.ErrorIs(subtest, write.err, errStub)
			checkFailedFencedFillReload(subtest, view, stub)
		})
	}
}

// TestFencedFillEvictionMatchesL1EvictionOnL2Hit pins review r12 F1 with two independent flights before either installs.
//
//nolint:paralleltest,tparallel // WHY: the rows share the decode pause used by Get's zero-value destination.
func TestFencedFillEvictionMatchesL1EvictionOnL2Hit(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	t.Run("fenced-fill", func(row *testing.T) { checkEvictedKeyL2Hit(row, true) })
	t.Run("direct-eviction", func(row *testing.T) { checkEvictedKeyL2Hit(row, false) })
}

// checkEvictedKeyL2Hit holds the two Gets after separate flights and releases them after the failed Set.
func checkEvictedKeyL2Hit(t *testing.T, fenced bool) {
	t.Helper()
	stub := newStubL2()
	view := NewTyped[pausedL1InstallValue](newTestTwoTier(t, stub), "n")
	epoch := prepareFailedSetEviction(t, view, stub)
	write, releaseWrite := startFailedEvictionSet(t, view, stub)
	requireFailedSetEviction(t, releaseWrite, view, epoch, fenced)

	gets := stub.getCalls.Load()
	first, releaseFirst := pauseL1InstallGet(t, view)
	require.Equal(t, gets+1, stub.getCalls.Load())
	second, releaseSecond := pauseL1InstallGet(t, view)
	require.Equal(t, gets+2, stub.getCalls.Load())
	_, cached, present := snapshotL2LockTestKey(t, releaseWrite, view, "n:a")
	require.False(t, present, "both Gets must still be before L1 install, found %d", cached)

	requireFailedEvictionSet(t, write, releaseWrite)
	releaseFirst()
	first.requireDone(t, "first Get did not finish after release")
	requireEvictedKeyInBothTiers(t, view, stub, 2)
	releaseSecond()
	second.requireDone(t, "second Get did not finish after release")
	requireEvictedKeyInBothTiers(t, view, stub, 2)
	require.Equal(t, gets+2, stub.getCalls.Load())
}

type pausedL1InstallValue int

type l1InstallPause struct {
	decodes  atomic.Int32
	entered  chan struct{}
	released chan struct{}
}

// l1InstallGetPause belongs to the sequential L2-hit rows and is used only by their value type.
var l1InstallGetPause atomic.Pointer[l1InstallPause]

// UnmarshalJSON pauses the second decode because it runs after singleflight, immediately before L1 install.
func (v *pausedL1InstallValue) UnmarshalJSON(raw []byte) error {
	var decoded int
	if err := json.Unmarshal(raw, &decoded); err != nil {
		return fmt.Errorf("cache/twotier: decode paused Get value: %w", err)
	}
	if pause := l1InstallGetPause.Load(); pause != nil && pause.decodes.Add(1) == 2 {
		close(pause.entered)
		<-pause.released
	}
	*v = pausedL1InstallValue(decoded)
	return nil
}

// pauseL1InstallGet waits for Get to leave its flight and reach the L1 decode.
func pauseL1InstallGet(t *testing.T, view *Typed[pausedL1InstallValue]) (*l2LockOperation, func()) {
	t.Helper()
	pause := &l1InstallPause{entered: make(chan struct{}), released: make(chan struct{})}
	l1InstallGetPause.Store(pause)
	t.Cleanup(func() { l1InstallGetPause.Store(nil) })
	release := sync.OnceFunc(func() { close(pause.released) })
	ctx := t.Context()
	get := startL2LockOperation(t, release, func() error { return readCachedL2LockTestKey(ctx, view, "a", 2) })
	waitL2LockSignal(t, pause.entered, "Get did not leave singleflight before L1 install")
	return get, release
}

// TestFencedFillEvictionMatchesL1EvictionOnL2Expiry pins review r12 F2 with an older loader value after expiry.
func TestFencedFillEvictionMatchesL1EvictionOnL2Expiry(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	t.Run("fenced-fill", func(row *testing.T) {
		row.Parallel()
		checkEvictedKeyL2Expiry(row, true)
	})
	t.Run("direct-eviction", func(row *testing.T) {
		row.Parallel()
		checkEvictedKeyL2Expiry(row, false)
	})
}

// checkEvictedKeyL2Expiry reaches the loader during the paused Set, then lets the reload cache its result.
func checkEvictedKeyL2Expiry(t *testing.T, fenced bool) {
	t.Helper()
	ctx := t.Context()
	stub := newStubL2()
	view := NewTyped[int](newTestTwoTier(t, stub), "n")
	epoch := prepareFailedSetEviction(t, view, stub)
	expire := startL2LockOperation(t, nil, func() error { return stub.Del(ctx, "n:a") })
	expire.requireDone(t, "L2 expiry did not finish")
	write, releaseWrite := startFailedEvictionSet(t, view, stub)
	requireFailedSetEviction(t, releaseWrite, view, epoch, fenced)

	gets := stub.getCalls.Load()
	var loads atomic.Int32
	entered := make(chan struct{})
	blocked := make(chan struct{})
	releaseLoader := sync.OnceFunc(func() { close(blocked) })
	release := func() {
		releaseWrite()
		releaseLoader()
	}
	reload := startL2LockOperation(t, release, func() error {
		value, err := view.Get(ctx, "a", func(context.Context) (int, error) {
			if loads.Add(1) == 1 {
				close(entered)
			}
			<-blocked
			return 1, nil
		})
		return checkL2LockTestValue(value, err, 1)
	})
	waitL2LockSignal(t, entered, "read after L2 expiry did not reach the loader during Set")
	requireFailedEvictionSet(t, write, releaseWrite)
	stub.failAfterHook = false
	releaseLoader()
	reload.requireDone(t, "reload did not finish after failed Set")
	require.EqualValues(t, 1, loads.Load())
	require.Equal(t, gets+1, stub.getCalls.Load())
	requireEvictedKeyInBothTiers(t, view, stub, 1)
}

// prepareFailedSetEviction captures the old fill's epoch and commits 2 before either eviction schedule.
func prepareFailedSetEviction[V ~int](t *testing.T, view *Typed[V], stub *stubL2) uint64 {
	t.Helper()
	ctx := t.Context()
	stub.data["n:a"] = []byte("1")
	epoch, _, present := snapshotL2LockTestKey(t, nil, view, "n:a")
	require.False(t, present)
	read := startL2LockOperation(t, nil, func() error { return readCachedL2LockTestKey(ctx, view, "a", 1) })
	read.requireDone(t, "initial Get did not finish")
	requireL2LockSetupSet(t, func() error { return view.Set(ctx, "a", 2) })
	committedEpoch, cached, present := snapshotL2LockTestKey(t, nil, view, "n:a")
	require.True(t, present)
	require.Equal(t, 2, int(cached))
	require.NotEqual(t, epoch, committedEpoch)
	return epoch
}

// startFailedEvictionSet leaves the pending fence active without changing the stub's L2 data.
func startFailedEvictionSet[V ~int](t *testing.T, view *Typed[V], stub *stubL2) (*l2LockOperation, func()) {
	t.Helper()
	entered, release, hook := blockL2Write(t, "set", "3")
	stub.beforeWrite = hook
	stub.failAfterHook = true
	ctx := t.Context()
	write := startL2LockOperation(t, release, func() error { return view.Set(ctx, "a", 3) })
	waitL2LockSignal(t, entered, "later Set did not enter L2")
	return write, release
}

// requireFailedSetEviction compares the old fill with an L1 TTL or capacity eviction at the same point.
func requireFailedSetEviction[V ~int](t *testing.T, release func(), view *Typed[V], epoch uint64, fenced bool) {
	t.Helper()
	if fenced {
		requireStaleL1InstallDone(t, release, view, epoch)
	} else {
		evict := startL2LockOperation(t, release, func() error {
			view.tt.mutation.Lock()
			defer view.tt.mutation.Unlock()
			view.tt.l1.Invalidate(view.key("a"))
			return nil
		})
		evict.requireDone(t, "direct L1 eviction did not finish")
	}
	_, cached, present := snapshotL2LockTestKey(t, release, view, "n:a")
	require.False(t, present, "eviction must leave L1 empty, found %d", cached)
}

// requireFailedEvictionSet verifies that releasing the writer clears the fence through its failure path.
func requireFailedEvictionSet(t *testing.T, write *l2LockOperation, release func()) {
	t.Helper()
	release()
	waitL2LockSignal(t, write.done, "later Set did not finish after release")
	require.ErrorIs(t, write.err, errStub)
}

// requireEvictedKeyInBothTiers inspects the installed values without triggering another read-through.
func requireEvictedKeyInBothTiers[V ~int](t *testing.T, view *Typed[V], stub *stubL2, want int) {
	t.Helper()
	_, cached, present := snapshotL2LockTestKey(t, nil, view, "n:a")
	require.True(t, present)
	require.Equal(t, want, int(cached))
	var raw string
	snapshot := startL2LockOperation(t, nil, func() error {
		stub.mu.Lock()
		defer stub.mu.Unlock()
		raw = string(stub.data["n:a"])
		return nil
	})
	snapshot.requireDone(t, "L2 snapshot did not finish")
	require.Equal(t, strconv.Itoa(want), raw)
}

// requireFencedFillEviction checks that the fenced fill evicted only its key.
func requireFencedFillEviction(t *testing.T, release func(), view *Typed[int]) {
	t.Helper()
	_, cached, kept := snapshotL2LockTestKey(t, release, view, "n:a")
	require.False(t, kept, "fenced fill must evict a, found %d in L1", cached)
	_, other, otherPresent := snapshotL2LockTestKey(t, release, view, "n:c")
	require.True(t, otherPresent, "unrelated key must stay warm in L1")
	require.Equal(t, 7, other)
}

// checkFailedFencedFillReload verifies the failed write leaves the key cold until one L2 read warms it again.
func checkFailedFencedFillReload(t *testing.T, view *Typed[int], stub *stubL2) {
	t.Helper()
	requireFencedFillEviction(t, nil, view)
	var raw string
	snapshot := startL2LockOperation(t, nil, func() error {
		stub.mu.Lock()
		defer stub.mu.Unlock()
		raw = string(stub.data["n:a"])
		return nil
	})
	snapshot.requireDone(t, "L2 snapshot did not finish")
	require.Equal(t, "2", raw)
	gets := stub.getCalls.Load()
	ctx := t.Context()
	read := func() error { return readCachedL2LockTestKey(ctx, view, "a", 2) }
	first := startL2LockOperation(t, nil, read)
	first.requireDone(t, "read after failed write did not finish")
	require.Equal(t, gets+1, stub.getCalls.Load(), "first read after failed write must reach L2")
	second := startL2LockOperation(t, nil, read)
	second.requireDone(t, "second read after failed write did not finish")
	require.Equal(t, gets+1, stub.getCalls.Load(), "second read after failed write must hit L1")
}

// snapshotL2LockTestKey bounds the read of the epoch and L1 value under mutation.
func snapshotL2LockTestKey[V ~int](t *testing.T, release func(), view *Typed[V], key string) (uint64, V, bool) {
	t.Helper()
	var epoch uint64
	var value V
	var present bool
	snapshot := startL2LockOperation(t, release, func() error {
		epoch, value, present = view.l1Snapshot(key)
		return nil
	})
	snapshot.requireDone(t, "L1 snapshot did not finish")
	return epoch, value, present
}

// checkFencedReloadCommit verifies the overlapping reload leaves the later Set in both tiers.
func checkFencedReloadCommit(t *testing.T, view *Typed[int], stub *stubL2) {
	t.Helper()
	_, cached, present := snapshotL2LockTestKey(t, nil, view, "n:a")
	require.True(t, present)
	require.Equal(t, 3, cached)
	var raw string
	snapshot := startL2LockOperation(t, nil, func() error {
		stub.mu.Lock()
		defer stub.mu.Unlock()
		raw = string(stub.data["n:a"])
		return nil
	})
	snapshot.requireDone(t, "L2 snapshot did not finish")
	require.Equal(t, "3", raw)
	gets := stub.getCalls.Load()
	ctx := t.Context()
	read := startL2LockOperation(t, nil, func() error { return readCachedL2LockTestKey(ctx, view, "a", 3) })
	read.requireDone(t, "read after later Set did not finish")
	require.Equal(t, gets, stub.getCalls.Load(), "read after later Set reached L2")
}

// warmFencedFillKeys preserves the miss epoch so the delayed fill can replay it.
func warmFencedFillKeys(t *testing.T, view *Typed[int]) uint64 {
	t.Helper()
	ctx := t.Context()
	epoch, _, ok := snapshotL2LockTestKey(t, nil, view, "n:a")
	require.False(t, ok)
	first := startL2LockOperation(t, nil, func() error { return fillL2LockTestKey(ctx, view) })
	first.requireDone(t, "warm read of a did not finish")
	other := startL2LockOperation(t, nil, func() error {
		value, err := view.Get(ctx, "c", func(context.Context) (int, error) { return 7, nil })
		return checkL2LockTestValue(value, err, 7)
	})
	other.requireDone(t, "warm read of c did not finish")
	warmEpoch, cached, present := snapshotL2LockTestKey(t, nil, view, "n:a")
	require.True(t, present)
	require.Equal(t, 1, cached)
	require.Equal(t, epoch, warmEpoch)
	return epoch
}

// moveFencedFillEpoch uses an unrelated notice so the pending write still fences the fill.
func moveFencedFillEpoch(t *testing.T, release func(), view *Typed[int], epoch uint64) {
	t.Helper()
	const unrelatedKey = "n:z"
	require.NotEqual(t, l2StripeIndex("n:a"), l2StripeIndex(unrelatedKey))
	var movedEpoch uint64
	notice := startL2LockOperation(t, release, func() error {
		view.tt.applyInvalidation(Invalidation{Origin: "peer", Kind: InvalidationKey, Key: unrelatedKey})
		movedEpoch, _, _ = view.l1Snapshot("n:a")
		return nil
	})
	notice.requireDone(t, "unrelated peer notice did not finish")
	require.NotEqual(t, epoch, movedEpoch)
}

// checkFencedFillReads uses L2 calls to distinguish a per-key eviction from an L1 hit or a full flush.
func checkFencedFillReads(t *testing.T, release func(), view *Typed[int], stub *stubL2) {
	t.Helper()
	ctx := t.Context()
	before := stub.getCalls.Load()
	read := startL2LockOperation(t, release, func() error { return readCachedL2LockTestKey(ctx, view, "a", 2) })
	read.requireDone(t, "read after fenced fill did not finish")
	require.Equal(t, before+1, stub.getCalls.Load(), "read after fenced fill must reach L2")
	other := startL2LockOperation(t, release, func() error { return readCachedL2LockTestKey(ctx, view, "c", 7) })
	other.requireDone(t, "read of unrelated key did not finish")
	require.Equal(t, before+1, stub.getCalls.Load(), "unrelated key must stay warm in L1")
}

// TestPanickingL2WriteClearsPendingRecord verifies that read-through can backfill L1 after a panic.
func TestPanickingL2WriteClearsPendingRecord(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	cases := []struct {
		name string
		op   string
		run  func(context.Context, *Typed[int]) error
	}{
		{name: "set", op: "set", run: func(ctx context.Context, view *Typed[int]) error {
			return view.Set(ctx, "a", 2)
		}},
		{name: "delete", op: "del", run: func(ctx context.Context, view *Typed[int]) error {
			return view.Delete(ctx, "a")
		}},
		{name: "prefix-delete", op: "del-prefix", run: func(ctx context.Context, view *Typed[int]) error {
			return view.InvalidatePrefix(ctx, "a")
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(subtest *testing.T) {
			subtest.Parallel()
			ctx := subtest.Context()
			stub := newStubL2()
			view := NewTyped[int](newTestTwoTier(subtest, stub), "n")
			var panicOnce sync.Once
			stub.beforeWrite = func(op, _ string, _ []byte) {
				if op == tc.op {
					panicOnce.Do(func() { panic("injected L2 write panic") })
				}
			}
			write := startL2LockOperation(subtest, nil, func() (result error) {
				defer func() {
					if cause := recover(); cause != nil {
						result = fmt.Errorf("cache/twotier: recovered L2 write panic: %v", cause)
					}
				}()
				return tc.run(ctx, view)
			})
			waitL2LockSignal(subtest, write.done, "panicking write did not finish")
			require.EqualError(subtest, write.err, "cache/twotier: recovered L2 write panic: injected L2 write panic")

			var loads int32
			read := func() error {
				value, err := view.Get(ctx, "a", func(context.Context) (int, error) {
					loads++
					return 1, nil
				})
				return checkL2LockTestValue(value, err, 1)
			}
			first := startL2LockOperation(subtest, nil, read)
			first.requireDone(subtest, "first read after panic did not finish")
			require.EqualValues(subtest, 1, loads, "origin loads on first read")
			gets := stub.getCalls.Load()
			second := startL2LockOperation(subtest, nil, read)
			second.requireDone(subtest, "second read after panic did not finish")
			require.Equal(subtest, gets, stub.getCalls.Load(), "second read after panic reached L2")
			require.EqualValues(subtest, 1, loads, "second read after panic reached the loader")
		})
	}
}

// writePeerL2Value bounds the peer's wait for the stub mutex during a paused write.
func writePeerL2Value(t *testing.T, stub *stubL2, key, raw string) {
	t.Helper()
	write := startL2LockOperation(t, nil, func() error {
		stub.mu.Lock()
		defer stub.mu.Unlock()
		stub.data[key] = []byte(raw)
		return nil
	})
	write.requireDone(t, "peer L2 write stalled behind the local L2 write")
}

// requireStaleL1InstallDone bounds the stale install attempt while the L2 write is paused.
func requireStaleL1InstallDone[V ~int](t *testing.T, release func(), view *Typed[V], epoch uint64) {
	t.Helper()
	install := startL2LockOperation(t, release, func() error {
		view.setL1IfCurrent("n:a", 1, epoch)
		return nil
	})
	install.requireDone(t, "stale L1 install attempt did not finish")
}

// TestPeerNoticeDuringSetWriteLeavesNoStaleL1 exercises write-through across peer invalidations.
func TestPeerNoticeDuringSetWriteLeavesNoStaleL1(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	cases := []struct {
		name       string
		notice     Invalidation
		want       int
		wantGets   int32
		peerWrites bool
	}{
		{name: "key-notice", notice: Invalidation{Origin: "peer", Kind: InvalidationKey, Key: "n:a"}, want: 2, wantGets: 1, peerWrites: true},
		{name: "no-notice", want: 1},
		{name: "prefix-notice", notice: Invalidation{Origin: "peer", Kind: InvalidationPrefix, Key: "n:"}, want: 2, wantGets: 1, peerWrites: true},
		{name: "unrelated-key-notice", notice: Invalidation{Origin: "peer", Kind: InvalidationKey, Key: "n:b"}, want: 1},
		{name: "non-covering-prefix-notice", notice: Invalidation{Origin: "peer", Kind: InvalidationPrefix, Key: "n:b"}, want: 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(subtest *testing.T) {
			subtest.Parallel()
			ctx := subtest.Context()
			stub := newStubL2()
			tiers := newTestTwoTier(subtest, stub)
			view := NewTyped[int](tiers, "n")
			entered, release, hook := blockL2Write(subtest, "set", "1")
			stub.afterWrite = hook
			set := startL2LockOperation(subtest, release, func() error { return view.Set(ctx, "a", 1) })
			waitL2LockSignal(subtest, entered, "Set did not write to L2")
			if tc.notice.Kind != "" {
				peer := startL2LockOperation(subtest, release, func() error {
					if tc.peerWrites {
						stub.mu.Lock()
						stub.data["n:a"] = []byte("2")
						stub.mu.Unlock()
					}
					tiers.applyInvalidation(tc.notice)
					return nil
				})
				peer.requireDone(subtest, "peer invalidation stalled behind the local L2 write")
			}
			release()
			set.requireDone(subtest, "Set did not finish after release")
			gets := stub.getCalls.Load()
			read := startL2LockOperation(subtest, nil, func() error { return readCachedL2LockTestKey(ctx, view, "a", tc.want) })
			read.requireDone(subtest, "read after Set did not finish")
			require.Equal(subtest, tc.wantGets, stub.getCalls.Load()-gets, "L2 reads after Set")
		})
	}
}

// TestL1OnlySetKeepsL1AcrossPeerNotice checks that a successful Set stays readable from L1.
func TestL1OnlySetKeepsL1AcrossPeerNotice(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	cases := []struct {
		name   string
		notice Invalidation
	}{
		{name: "same-key-notice", notice: Invalidation{Origin: "peer", Kind: InvalidationKey, Key: "n:a"}},
		{name: "unrelated-key-notice", notice: Invalidation{Origin: "peer", Kind: InvalidationKey, Key: "n:b"}},
		{name: "prefix-notice", notice: Invalidation{Origin: "peer", Kind: InvalidationPrefix, Key: "n:"}},
		{name: "no-notice"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(subtest *testing.T) {
			subtest.Parallel()
			ctx := subtest.Context()
			entered, release, hook := blockL2Write(subtest, "set", "1")
			stub := &hookedNoneL2{afterWrite: hook}
			tiers := newTestTwoTier(subtest, stub)
			tiers.l1Only = true
			view := NewTyped[int](tiers, "n")
			set := startL2LockOperation(subtest, release, func() error { return view.Set(ctx, "a", 1) })
			waitL2LockSignal(subtest, entered, "Set did not reach the L2 hook")
			if tc.notice.Kind != "" {
				peer := startL2LockOperation(subtest, release, func() error {
					tiers.applyInvalidation(tc.notice)
					return nil
				})
				peer.requireDone(subtest, "peer invalidation stalled behind the local L2 write")
			}
			release()
			set.requireDone(subtest, "Set did not finish after release")
			gets := stub.getCalls.Load()
			read := startL2LockOperation(subtest, nil, func() error { return readCachedL2LockTestKey(ctx, view, "a", 1) })
			read.requireDone(subtest, "read after Set did not finish")
			require.Equal(subtest, gets, stub.getCalls.Load(), "read after Set reached L2")
		})
	}
}

// TestL1OnlyFillKeepsCommittedL1DuringWrite guards warm L1 hits against delayed fills.
func TestL1OnlyFillKeepsCommittedL1DuringWrite(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	cases := [...]struct {
		op  string
		raw string
		run func(context.Context, *Typed[int]) error
	}{
		{op: "set", raw: "3", run: func(ctx context.Context, view *Typed[int]) error {
			return view.Set(ctx, "a", 3)
		}},
		{op: "del", run: func(ctx context.Context, view *Typed[int]) error {
			return view.Delete(ctx, "a")
		}},
		{op: "del-prefix", run: func(ctx context.Context, view *Typed[int]) error {
			return view.InvalidatePrefix(ctx, "a")
		}},
	}
	for _, tc := range cases {
		t.Run(tc.op, func(subtest *testing.T) {
			subtest.Parallel()
			ctx := subtest.Context()
			entered, release, hook := blockL2Write(subtest, tc.op, tc.raw)
			stub := &hookedNoneL2{afterWrite: hook}
			tiers := newTestTwoTier(subtest, stub)
			tiers.l1Only = true
			view := NewTyped[int](tiers, "n")
			epoch0, _, ok := view.l1Snapshot("n:a")
			require.False(subtest, ok)
			requireL2LockSetupSet(subtest, func() error { return view.Set(ctx, "a", 2) })

			write := startL2LockOperation(subtest, release, func() error { return tc.run(ctx, view) })
			waitL2LockSignal(subtest, entered, "write did not reach the L2 hook")
			requireStaleL1InstallDone(subtest, release, view, epoch0)
			requireL1OnlyCachedRead(subtest, release, view, stub, 2)

			release()
			write.requireDone(subtest, "write did not finish after release")
			if tc.op == "set" {
				requireL1OnlyCachedRead(subtest, nil, view, stub, 3)
				return
			}
			_, _, present := view.l1Snapshot("n:a")
			require.False(subtest, present, "delete did not remove the L1 value")
		})
	}
}

// requireL1OnlyCachedRead bounds a read and verifies that it stayed in L1.
func requireL1OnlyCachedRead(t *testing.T, release func(), view *Typed[int], stub *hookedNoneL2, want int) {
	t.Helper()
	ctx := t.Context()
	gets := stub.getCalls.Load()
	read := startL2LockOperation(t, release, func() error { return readCachedL2LockTestKey(ctx, view, "a", want) })
	read.requireDone(t, "L1-only read did not finish")
	require.Equal(t, gets, stub.getCalls.Load(), "L1-only read reached L2")
}

// hookedNoneL2 records reads and pauses writes while retaining noneL2 semantics.
type hookedNoneL2 struct {
	noneL2
	getCalls   atomic.Int32
	afterWrite func(op, key string, val []byte)
}

// Get counts read attempts so tests can verify that L1 serves the value.
func (s *hookedNoneL2) Get(ctx context.Context, key string) ([]byte, bool, error) {
	s.getCalls.Add(1)
	return s.noneL2.Get(ctx, key)
}

// Set pauses the caller so a test can deliver a peer notice before the L1 commit.
func (s *hookedNoneL2) Set(_ context.Context, key string, val []byte, _ time.Duration) error {
	s.afterWrite("set", key, val)
	return nil
}

// Del pauses key deletion before it commits to L1.
func (s *hookedNoneL2) Del(ctx context.Context, key string) error {
	s.afterWrite("del", key, nil)
	return s.noneL2.Del(ctx, key)
}

// DelPrefix pauses prefix deletion while preserving noneL2 refusal.
func (s *hookedNoneL2) DelPrefix(ctx context.Context, prefix string) error {
	s.afterWrite("del-prefix", prefix, nil)
	return s.noneL2.DelPrefix(ctx, prefix)
}

// TestL2StripeIndex checks that boundary keys select valid, reproducible stripes.
func TestL2StripeIndex(t *testing.T) {
	t.Parallel()
	for _, key := range []string{"", "a", strings.Repeat("a", 4096)} {
		index := l2StripeIndex(key)
		require.GreaterOrEqual(t, index, 0, "key length %d", len(key))
		require.Less(t, index, 64, "key length %d", len(key))
		require.Equal(t, index, l2StripeIndex(strings.Clone(key)), "key length %d", len(key))
	}
}

// blockL2Write targets the test key shared by the existing lock scenarios.
func blockL2Write(t *testing.T, operation, raw string) (<-chan struct{}, func(), func(string, string, []byte)) {
	t.Helper()
	return blockL2WriteForKey(t, operation, "n:a", raw)
}

// blockL2WriteForKey pauses a matching call outside the stub mutex so other calls can proceed.
func blockL2WriteForKey(t *testing.T, operation, target, raw string) (<-chan struct{}, func(), func(string, string, []byte)) {
	t.Helper()
	entered := make(chan struct{})
	signal := sync.OnceFunc(func() { close(entered) })
	blocked := make(chan struct{})
	release := sync.OnceFunc(func() { close(blocked) })
	t.Cleanup(release)
	hook := func(op, key string, val []byte) {
		if op == operation && key == target && string(val) == raw {
			signal()
			<-blocked
		}
	}
	return entered, release, hook
}

const l2LockTimeout = 2 * time.Second

// l2LockTestDeadline leaves room above the 2 s worker bound and below the 4 m package timeout.
const l2LockTestDeadline = 90 * time.Second

// armL2LockWatchdog bounds synchronous lock calls and covers the test's subtests.
func armL2LockWatchdog(t *testing.T) {
	t.Helper()
	armL2LockWatchdogFor(t, l2LockTestDeadline, panicL2LockWatchdog)
}

// armL2LockWatchdogFor captures all goroutine stacks when a test outlives its deadline.
func armL2LockWatchdogFor(t *testing.T, deadline time.Duration, expire func(string)) {
	t.Helper()
	name := t.Name()
	timer := time.AfterFunc(deadline, func() {
		stack := make([]byte, 1<<20)
		size := runtime.Stack(stack, true)
		expire(fmt.Sprintf("cache/twotier: %s still running after %s: a synchronous call is blocked on mutation\n%s",
			name, deadline, stack[:size]))
	})
	t.Cleanup(func() { timer.Stop() })
}

// panicL2LockWatchdog ends the test binary when a synchronous mutex wait cannot be interrupted.
func panicL2LockWatchdog(message string) {
	panic(message)
}

type l2LockOperation struct {
	done        chan struct{}
	stackPrefix string
	err         error
}

var errL2LockWorkerExited = errors.New("cache/twotier: test worker exited before its operation returned")

// startL2LockOperation registers cleanup before waiting for the background operation.
func startL2LockOperation(t *testing.T, release func(), run func() error) *l2LockOperation {
	t.Helper()
	started := make(chan struct{})
	op := &l2LockOperation{done: make(chan struct{})}
	go func() {
		returned := false
		defer func() {
			if !returned {
				op.err = errL2LockWorkerExited
			}
			close(op.done)
		}()
		var stack [128]byte
		size := runtime.Stack(stack[:], false)
		prefix, _, _ := bytes.Cut(stack[:size], []byte(" ["))
		op.stackPrefix = "\n" + string(prefix) + " ["
		close(started)
		op.err = run()
		returned = true
	}()
	t.Cleanup(func() {
		if release != nil {
			release()
		}
		waitL2LockSignal(t, op.done, "operation did not stop during cleanup")
	})
	waitL2LockSignal(t, started, "operation goroutine did not start")
	return op
}

// TestL2LockOperationReportsWorkerExit distinguishes Goexit from either kind of return.
func TestL2LockOperationReportsWorkerExit(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	wrapped := fmt.Errorf("cache/twotier: planted worker failure: %w", errL2LockWorkerExited)
	cases := [...]struct {
		name string
		run  func() error
		want error
	}{
		{name: "goexit", run: func() error {
			runtime.Goexit()
			return nil
		}, want: errL2LockWorkerExited},
		{name: "nil-return", run: func() error { return nil }},
		{name: "wrapped-error-return", run: func() error { return wrapped }, want: wrapped},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(subtest *testing.T) {
			subtest.Parallel()
			op := startL2LockOperation(subtest, nil, tc.run)
			waitL2LockSignal(subtest, op.done, "worker operation did not finish")
			require.ErrorIs(subtest, op.err, tc.want)
			if tc.want != nil {
				require.Same(subtest, tc.want, op.err)
			}
		})
	}
}

// requireL2LockSetupSet keeps a stalled setup Set within the operation timeout.
func requireL2LockSetupSet(t *testing.T, run func() error) {
	t.Helper()
	setup := startL2LockOperation(t, nil, run)
	setup.requireDone(t, "setup Set did not finish")
}

// requireDone synchronizes with the operation before reading its error.
func (op *l2LockOperation) requireDone(t *testing.T, timeoutMessage string) {
	t.Helper()
	waitL2LockSignal(t, op.done, timeoutMessage)
	require.NoError(t, op.err)
}

// requireBlocked observes the competing goroutine parked on the expected lock.
func (op *l2LockOperation) requireBlocked(t *testing.T, waitReason, frame string) {
	t.Helper()
	const pollInterval = 2 * time.Millisecond
	polls := time.NewTicker(pollInterval)
	defer polls.Stop()
	deadline := time.After(l2LockTimeout)
	stack := make([]byte, 256<<10)
	blocked := false
poll:
	for range int(l2LockTimeout / pollInterval) {
		select {
		case <-op.done:
			t.Fatalf("operation finished before blocking in %s", frame)
		case <-deadline:
			break poll
		case <-polls.C:
			if op.blockedIn(stack, waitReason, frame) {
				blocked = true
				break poll
			}
		}
	}
	require.Truef(t, blocked, "operation did not block on %s in %s", waitReason, frame)
	select {
	case <-op.done:
		t.Fatalf("operation finished while its lock was still held in %s", frame)
	default:
	}
}

// blockedIn matches the wait reason and frame in this operation's own traceback.
func (op *l2LockOperation) blockedIn(stack []byte, waitReason, frame string) bool {
	size := runtime.Stack(stack, true)
	_, trace, found := bytes.Cut(stack[:size], []byte(op.stackPrefix))
	if !found {
		return false
	}
	trace, _, _ = bytes.Cut(trace, []byte("\n\n"))
	reason, frames, _ := bytes.Cut(trace, []byte("\n"))
	return bytes.HasPrefix(reason, []byte(waitReason+"]")) && bytes.Contains(frames, []byte(frame))
}

// waitL2LockSignal bounds test and cleanup waits for worker signals.
func waitL2LockSignal(t *testing.T, signal <-chan struct{}, timeoutMessage string) {
	t.Helper()
	if !l2LockSignalReceived(signal, l2LockTimeout) {
		t.Fatal(timeoutMessage)
	}
}

// l2LockSignalReceived lets worker hooks time out without touching testing.T.
func l2LockSignalReceived(signal <-chan struct{}, timeout time.Duration) bool {
	select {
	case <-signal:
		return true
	case <-time.After(timeout):
		return false
	}
}

// fillL2LockTestKey uses a fixed value so the stub can distinguish a fill from a Set.
func fillL2LockTestKey(ctx context.Context, view *Typed[int]) error {
	value, err := view.Get(ctx, "a", func(context.Context) (int, error) { return 1, nil })
	return checkL2LockTestValue(value, err, 1)
}

// readCachedL2LockTestKey treats an origin load as a failure of the expected cache hit.
func readCachedL2LockTestKey[V ~int](ctx context.Context, view *Typed[V], key string, want int) error {
	value, err := view.Get(ctx, key, func(context.Context) (V, error) {
		return 0, errors.New("cache/twotier: loader ran for cached key")
	})
	return checkL2LockTestValue(int(value), err, want)
}

// checkL2LockTestValue reports a failed read without terminating the worker goroutine.
func checkL2LockTestValue(value int, err error, want int) error {
	if err != nil {
		return fmt.Errorf("cache/twotier: read test key: %w", err)
	}
	if value != want {
		return fmt.Errorf("cache/twotier: got %d, want %d", value, want)
	}
	return nil
}
