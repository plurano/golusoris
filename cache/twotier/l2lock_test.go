// SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
//
// SPDX-License-Identifier: EUPL-1.2

package twotier

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
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
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			ctx := t.Context()
			stub := newStubL2()
			view := NewTyped[int](newTestTwoTier(t, stub), "n")
			require.NoError(t, view.Set(ctx, "b", 7))
			if tc.corrupt {
				stub.data["n:a"] = []byte("not-json")
			}
			entered, release := blockL2Write(t, stub, tc.op, tc.raw)
			write := startL2LockOperation(t, release, func() error { return tc.run(ctx, view) })
			waitL2LockSignal(t, entered, "L2 write did not enter the stub")
			gets := stub.getCalls.Load()
			read := startL2LockOperation(t, release, func() error { return readWarmL2LockTestKey(ctx, view, "b", 7) })
			read.requireDone(t, "warm L1 read stalled behind blocked L2 call")
			require.Equal(t, gets, stub.getCalls.Load(), "warm read reached L2")
			release()
			write.requireDone(t, "L2 operation did not finish after release")
		})
	}
}

// TestLoaderFillOrderedBeforeSameKeySet protects explicit writes against older fills.
func TestLoaderFillOrderedBeforeSameKeySet(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	stub := newStubL2()
	view := NewTyped[int](newTestTwoTier(t, stub), "n")
	entered, release := blockL2Write(t, stub, "set", "1")
	fill := startL2LockOperation(t, release, func() error { return fillL2LockTestKey(ctx, view) })
	waitL2LockSignal(t, entered, "loader fill did not enter L2")
	set := startL2LockOperation(t, release, func() error { return view.Set(ctx, "a", 2) })
	set.expectPending(t, "same-key Set finished before the loader fill was released")
	release()
	fill.requireDone(t, "loader fill did not finish after release")
	set.requireDone(t, "same-key Set did not finish after release")
	value, ok, err := stub.Get(ctx, "n:a")
	require.NoError(t, err)
	require.True(t, ok)
	require.Equal(t, "2", string(value))
	require.NoError(t, readWarmL2LockTestKey(ctx, view, "a", 2))
}

// TestPrefixInvalidationWaitsForInFlightFill protects prefix deletes against older fills.
func TestPrefixInvalidationWaitsForInFlightFill(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	stub := newStubL2()
	view := NewTyped[int](newTestTwoTier(t, stub), "n")
	entered, release := blockL2Write(t, stub, "set", "1")
	fill := startL2LockOperation(t, release, func() error { return fillL2LockTestKey(ctx, view) })
	waitL2LockSignal(t, entered, "loader fill did not enter L2")
	invalidate := startL2LockOperation(t, release, func() error { return view.InvalidatePrefix(ctx, "") })
	invalidate.expectPending(t, "prefix invalidation finished before the loader fill was released")
	release()
	fill.requireDone(t, "loader fill did not finish after release")
	invalidate.requireDone(t, "prefix invalidation did not finish after release")
	require.Empty(t, stub.data)
	loaded := false
	value, err := view.Get(ctx, "a", func(context.Context) (int, error) {
		loaded = true
		return 2, nil
	})
	require.NoError(t, err)
	require.True(t, loaded)
	require.Equal(t, 2, value)
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
func blockL2Write(t *testing.T, stub *stubL2, operation, raw string) (<-chan struct{}, func()) {
	t.Helper()
	entered := make(chan struct{})
	signal := sync.OnceFunc(func() { close(entered) })
	blocked := make(chan struct{})
	release := sync.OnceFunc(func() { close(blocked) })
	t.Cleanup(release)
	stub.beforeWrite = func(op, key string, val []byte) {
		if op == operation && key == "n:a" && string(val) == raw {
			signal()
			<-blocked
		}
	}
	return entered, release
}

type l2LockOperation struct {
	done chan struct{}
	err  error
}

// startL2LockOperation registers cleanup before waiting for the background operation.
func startL2LockOperation(t *testing.T, release func(), run func() error) *l2LockOperation {
	t.Helper()
	started := make(chan struct{})
	op := &l2LockOperation{done: make(chan struct{})}
	go func() {
		defer close(op.done)
		close(started)
		op.err = run()
	}()
	t.Cleanup(func() {
		release()
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

// expectPending gives an unordered operation time to overtake the blocked write.
func (op *l2LockOperation) expectPending(t *testing.T, message string) {
	t.Helper()
	select {
	case <-op.done:
		t.Error(message)
	case <-time.After(25 * time.Millisecond):
	}
}

// waitL2LockSignal bounds test and cleanup waits for worker signals.
func waitL2LockSignal(t *testing.T, signal <-chan struct{}, timeoutMessage string) {
	t.Helper()
	select {
	case <-signal:
	case <-time.After(2 * time.Second):
		t.Fatal(timeoutMessage)
	}
}

// fillL2LockTestKey uses a fixed value so the stub can distinguish a fill from a Set.
func fillL2LockTestKey(ctx context.Context, view *Typed[int]) error {
	value, err := view.Get(ctx, "a", func(context.Context) (int, error) { return 1, nil })
	return checkL2LockTestValue(value, err, 1)
}

// readWarmL2LockTestKey treats an origin load as a failure of the expected cache hit.
func readWarmL2LockTestKey(ctx context.Context, view *Typed[int], key string, want int) error {
	value, err := view.Get(ctx, key, func(context.Context) (int, error) {
		return 0, errors.New("cache/twotier: loader ran for warm key")
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
