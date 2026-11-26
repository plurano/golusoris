// SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
//
// SPDX-License-Identifier: EUPL-1.2

package twotier

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"runtime"
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
			setup := startL2LockOperation(subtest, nil, func() error { return view.Set(ctx, "b", 7) })
			setup.requireDone(subtest, "setup Set did not finish")
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
	view.setL1IfCurrent("n:a", 1, epoch)
	second := startL2LockOperation(t, release, func() error { return readCachedL2LockTestKey(ctx, view, "a", 2) })
	second.requireDone(t, "read after stale install attempt did not finish")

	release()
	set.requireDone(t, "Set did not finish after release")
	gets := stub.getCalls.Load()
	last := startL2LockOperation(t, nil, func() error { return readCachedL2LockTestKey(ctx, view, "a", 2) })
	last.requireDone(t, "read after Set did not finish")
	require.Equal(t, gets, stub.getCalls.Load(), "read after Set reached L2")
}

// TestPeerNoticeDuringSetWriteLeavesNoStaleL1 exercises write-through across peer invalidations.
func TestPeerNoticeDuringSetWriteLeavesNoStaleL1(t *testing.T) {
	t.Parallel()
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

// blockL2Write pauses one matching call outside the stub mutex so other calls can proceed.
func blockL2Write(t *testing.T, operation, raw string) (<-chan struct{}, func(), func(string, string, []byte)) {
	t.Helper()
	entered := make(chan struct{})
	signal := sync.OnceFunc(func() { close(entered) })
	blocked := make(chan struct{})
	release := sync.OnceFunc(func() { close(blocked) })
	t.Cleanup(release)
	hook := func(op, key string, val []byte) {
		if op == operation && key == "n:a" && string(val) == raw {
			signal()
			<-blocked
		}
	}
	return entered, release, hook
}

const l2LockTimeout = 2 * time.Second

type l2LockOperation struct {
	done        chan struct{}
	stackPrefix string
	err         error
}

// startL2LockOperation registers cleanup before waiting for the background operation.
func startL2LockOperation(t *testing.T, release func(), run func() error) *l2LockOperation {
	t.Helper()
	started := make(chan struct{})
	op := &l2LockOperation{done: make(chan struct{})}
	go func() {
		defer close(op.done)
		var stack [128]byte
		size := runtime.Stack(stack[:], false)
		prefix, _, _ := bytes.Cut(stack[:size], []byte(" ["))
		op.stackPrefix = "\n" + string(prefix) + " ["
		close(started)
		op.err = run()
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
	select {
	case <-signal:
	case <-time.After(l2LockTimeout):
		t.Fatal(timeoutMessage)
	}
}

// fillL2LockTestKey uses a fixed value so the stub can distinguish a fill from a Set.
func fillL2LockTestKey(ctx context.Context, view *Typed[int]) error {
	value, err := view.Get(ctx, "a", func(context.Context) (int, error) { return 1, nil })
	return checkL2LockTestValue(value, err, 1)
}

// readCachedL2LockTestKey treats an origin load as a failure of the expected cache hit.
func readCachedL2LockTestKey(ctx context.Context, view *Typed[int], key string, want int) error {
	value, err := view.Get(ctx, key, func(context.Context) (int, error) {
		return 0, errors.New("cache/twotier: loader ran for cached key")
	})
	return checkL2LockTestValue(value, err, want)
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
