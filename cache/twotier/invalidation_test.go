// SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
//
// SPDX-License-Identifier: EUPL-1.2

package twotier

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/redis/rueidis"
	"github.com/stretchr/testify/require"

	"github.com/golusoris/golusoris/cache/memory"
	"github.com/golusoris/golusoris/realtime/pubsub"
)

// newPeer builds one replica: its own L1, the shared l2, and broadcaster b
// (nil disables invalidation).
func newPeer(t *testing.T, shared l2, b Broadcaster) *TwoTier {
	t.Helper()
	l1, err := memory.NewForTest(100, 0)
	require.NoError(t, err)
	opts := Options{L2: L2None, L1TTL: time.Hour}
	var options []Option
	if b != nil {
		opts.Invalidation = InvalidationOptions{Enabled: true}
		options = append(options, WithBroadcaster(b))
	}
	tt, err := New(l1, opts, slog.New(slog.DiscardHandler), options...)
	require.NoError(t, err)
	tt.l2 = shared
	// The shared L2 stub models the Redis-mode Set fence.
	tt.l1Only = false
	return tt
}

// newPeers returns two listening replicas sharing one L2 and one in-process bus.
func newPeers(t *testing.T) (a, b *TwoTier, shared *stubL2) {
	t.Helper()
	return newPeersWithBroadcastSignal(t, nil)
}

// newPeersWithBroadcastSignal can observe broadcasts from the second replica.
func newPeersWithBroadcastSignal(t *testing.T, signal func()) (a, b *TwoTier, shared *stubL2) {
	t.Helper()
	return newPeersWithBroadcastSignals(t, nil, signal)
}

// newPeersWithBroadcastSignals observes broadcast entry on either replica.
func newPeersWithBroadcastSignals(t *testing.T, signalA, signalB func()) (a, b *TwoTier, shared *stubL2) {
	t.Helper()
	shared = newStubL2()
	broadcaster, err := NewBusBroadcaster(pubsub.New(), "", nil)
	require.NoError(t, err)
	a = newPeer(t, shared, broadcasterWithSignal(broadcaster, signalA))
	b = newPeer(t, shared, broadcasterWithSignal(broadcaster, signalB))
	t.Cleanup(a.Listen())
	t.Cleanup(b.Listen())
	return a, b, shared
}

// broadcasterWithSignal wraps only broadcasters whose entry is observed by a test.
func broadcasterWithSignal(broadcaster Broadcaster, signal func()) Broadcaster {
	if signal == nil {
		return broadcaster
	}
	return &signalingBroadcaster{Broadcaster: broadcaster, signal: signal}
}

type signalingBroadcaster struct {
	Broadcaster
	signal func()
}

// Broadcast signals entry before delegating to the synchronous bus.
func (b *signalingBroadcaster) Broadcast(ctx context.Context, inv Invalidation) error {
	b.signal()
	if err := b.Broadcaster.Broadcast(ctx, inv); err != nil {
		return fmt.Errorf("cache/twotier: test broadcast: %w", err)
	}
	return nil
}

// cacheOn makes peer hold 1 for key "k" in its L1.
func cacheOn(t *testing.T, peer *TwoTier) {
	t.Helper()
	got, err := NewTyped[int](peer, "n").Get(context.Background(), "k", func(context.Context) (int, error) {
		return 1, nil
	})
	require.NoError(t, err)
	require.Equal(t, 1, got)
}

func TestInvalidation_DeleteEvictsPeerL1(t *testing.T) {
	t.Parallel()
	a, b, _ := newPeers(t)
	cacheOn(t, b)
	require.NoError(t, NewTyped[int](a, "n").Delete(context.Background(), "k"))
	var loads atomic.Int32
	value, err := NewTyped[int](b, "n").Get(context.Background(), "k", countingLoader(&loads, 2))
	require.NoError(t, err)
	require.Equal(t, 2, value)
	require.EqualValues(t, 1, loads.Load(), "peer reloads after the remote delete")
}

// TestInvalidation_WithoutBroadcasterPeerStaysStale is the negative control:
// without invalidation the peer serves its old L1 value until L1 TTL.
func TestInvalidation_WithoutBroadcasterPeerStaysStale(t *testing.T) {
	t.Parallel()
	shared := newStubL2()
	a, b := newPeer(t, shared, nil), newPeer(t, shared, nil)
	cacheOn(t, b)
	require.NoError(t, NewTyped[int](a, "n").Delete(context.Background(), "k"))
	var loads atomic.Int32
	value, err := NewTyped[int](b, "n").Get(context.Background(), "k", countingLoader(&loads, 2))
	require.NoError(t, err)
	require.Equal(t, 1, value)
	require.Zero(t, loads.Load())
}

func TestInvalidation_SetMakesPeerReadNewValue(t *testing.T) {
	t.Parallel()
	a, b, _ := newPeers(t)
	cacheOn(t, b)
	require.NoError(t, NewTyped[int](a, "n").Set(context.Background(), "k", 7))
	var loads atomic.Int32
	value, err := NewTyped[int](b, "n").Get(context.Background(), "k", countingLoader(&loads, 0))
	require.NoError(t, err)
	require.Equal(t, 7, value, "peer reads the new value from L2")
	require.Zero(t, loads.Load())
}

// TestInvalidation_PeerSetDuringLocalSetLeavesNoStaleL1 checks the shared-L2 Set fence through pubsub.
func TestInvalidation_PeerSetDuringLocalSetLeavesNoStaleL1(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	ctx := t.Context()
	a, b, shared := newPeers(t)
	aView := NewTyped[int](a, "n")
	bView := NewTyped[int](b, "n")
	entered, release, hook := blockL2Write(t, "set", "1")
	shared.afterWrite = hook

	localSet := startL2LockOperation(t, release, func() error { return aView.Set(ctx, "a", 1) })
	waitL2LockSignal(t, entered, "local Set did not write to L2")
	peerSet := startL2LockOperation(t, release, func() error { return bView.Set(ctx, "a", 2) })
	// LocalBus delivers the peer notice synchronously before Set returns.
	peerSet.requireDone(t, "peer Set did not finish while local Set was paused")
	release()
	localSet.requireDone(t, "local Set did not finish after release")

	gets := shared.getCalls.Load()
	read := startL2LockOperation(t, nil, func() error { return readCachedL2LockTestKey(ctx, aView, "a", 2) })
	read.requireDone(t, "read after local Set did not finish")
	require.Equal(t, int32(1), shared.getCalls.Load()-gets, "L2 reads after local Set")
}

// TestWriterReplicaReadsPendingL2Value checks the warm writer after a cold peer reads its pending Set.
func TestWriterReplicaReadsPendingL2Value(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	ctx := t.Context()
	a, b, shared := newPeers(t)
	cacheOn(t, a)
	aView, bView := NewTyped[int](a, "n"), NewTyped[int](b, "n")
	entered, release, hook := blockL2WriteForKey(t, "set", "n:k", "2")
	shared.afterWrite = hook

	set := startL2LockOperation(t, release, func() error { return aView.Set(ctx, "k", 2) })
	waitL2LockSignal(t, entered, "local Set did not publish to L2")
	peerRead := startL2LockOperation(t, release, func() error { return readCachedL2LockTestKey(ctx, bView, "k", 2) })
	peerRead.requireDone(t, "cold peer did not read the pending L2 value")
	localRead := startL2LockOperation(t, release, func() error { return readCachedL2LockTestKey(ctx, aView, "k", 2) })
	localRead.requireDone(t, "warm writer did not read the pending L2 value")
	requireOperationRunning(t, set, "local Set returned before its L2 hook was released")
	release()
	set.requireDone(t, "local Set did not finish after release")
	committedRead := startL2LockOperation(t, nil, func() error { return readCachedL2LockTestKey(ctx, aView, "k", 2) })
	committedRead.requireDone(t, "local Get after Set did not finish")
}

type localMutationCase struct {
	name   string
	prefix bool
	mutate func(context.Context, *Typed[int]) error
}

// TestLocalMutationWaitsForOlderFill checks the local barrier before a synchronous peer Delete.
func TestLocalMutationWaitsForOlderFill(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	cases := [...]localMutationCase{
		{name: "set", mutate: func(ctx context.Context, view *Typed[int]) error {
			return view.Set(ctx, "z", 2)
		}},
		{name: "delete", mutate: func(ctx context.Context, view *Typed[int]) error {
			return view.Delete(ctx, "z")
		}},
		{name: "prefix", prefix: true, mutate: func(ctx context.Context, view *Typed[int]) error {
			return view.InvalidatePrefix(ctx, "z")
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(subtest *testing.T) {
			subtest.Parallel()
			require.NotEqual(subtest, l2StripeIndex("n:k"), l2StripeIndex("n:z"))
			checkLocalMutationAfterBlockedFill(subtest, tc)
		})
	}
}

// checkLocalMutationAfterBlockedFill holds a fill gate reader until the local barrier queues.
func checkLocalMutationAfterBlockedFill(t *testing.T, tc localMutationCase) {
	t.Helper()
	ctx := t.Context()
	broadcastStarted := make(chan struct{})
	a, b, shared := newPeersWithBroadcastSignals(t, sync.OnceFunc(func() { close(broadcastStarted) }), nil)
	aView, bView := NewTyped[int](a, "n"), NewTyped[int](b, "n")
	entered, releaseFill, fillHook := blockL2WriteForKey(t, "set", "n:k", "1")
	shared.beforeWrite = fillHook
	fill := startOlderPeerFill(t, releaseFill, aView)
	waitL2LockSignal(t, entered, "loader fill did not reach its L2 write")
	releaseReader := releaseFill
	if tc.prefix {
		// A separate reader isolates the prefix barrier after the prefix lock drains the real fill.
		require.True(t, a.l2FillGate.TryRLock(), "could not hold a fill gate reader during the prefix commit")
		releaseReader = sync.OnceFunc(a.l2FillGate.RUnlock)
		defer releaseReader()
		releaseFill()
		fill.requireDone(t, "loader fill did not finish before the prefix write")
	}
	epoch, _ := localMutationSnapshot(a, "n:z", tc.prefix)
	mutation := startL2LockOperation(t, releaseReader, func() error {
		if err := tc.mutate(ctx, aView); err != nil {
			return fmt.Errorf("cache/twotier: local %s: %w", tc.name, err)
		}
		select {
		case <-broadcastStarted:
			return nil
		default:
			return fmt.Errorf("cache/twotier: local %s returned before entering broadcast", tc.name)
		}
	})
	requireFillGateWriter(t, &a.l2FillGate, func(queued bool) {
		requireOperationRunning(t, mutation, "local mutation returned before the older fill barrier opened")
		if queued {
			requireLocalMutationCommitted(t, a, tc, epoch, broadcastStarted)
		}
	}, "local mutation never queued on the fill gate")
	releaseReader()
	fill.requireDone(t, "loader fill did not finish after release")
	mutation.requireDone(t, "local mutation did not finish after release")
	peer := startL2LockOperation(t, nil, func() error { return bView.Delete(ctx, "k") })
	peer.requireDone(t, "peer Delete did not finish")
	_, found, err := shared.Get(ctx, "n:k")
	require.NoError(t, err)
	require.False(t, found, "older fill recreated the deleted L2 entry")
	requirePeerReload(t, ctx, aView)
}

// requireLocalMutationCommitted checks that the queued barrier follows commit and precedes broadcast.
func requireLocalMutationCommitted(t *testing.T, tt *TwoTier, tc localMutationCase, before uint64, broadcast <-chan struct{}) {
	t.Helper()
	epoch, pending := localMutationSnapshot(tt, "n:z", tc.prefix)
	require.Greater(t, epoch, before, "local %s queued its fill barrier before committing its epoch", tc.name)
	require.False(t, pending, "local %s queued its fill barrier with its pending write still active", tc.name)
	select {
	case <-broadcast:
		t.Fatalf("local %s entered broadcast before its fill barrier opened", tc.name)
	default:
	}
}

// localMutationSnapshot reads the committed epoch and relevant pending slot under mutation.
func localMutationSnapshot(tt *TwoTier, key string, prefix bool) (uint64, bool) {
	tt.mutation.RLock()
	defer tt.mutation.RUnlock()
	if prefix {
		return tt.epoch, tt.prefixPending.active
	}
	return tt.epoch, tt.l2Pending[l2StripeIndex(key)].active
}

// requireOperationRunning checks a worker has yet to return at a held test checkpoint.
func requireOperationRunning(t *testing.T, op *l2LockOperation, message string) {
	t.Helper()
	select {
	case <-op.done:
		t.Fatal(message)
	default:
	}
}

// TestLocalWriteBarrierLimit reproduces the #1609 end state after a peer Delete.
func TestLocalWriteBarrierLimit(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	cases := [...]struct {
		name   string
		failed bool
	}{
		{name: "barrier-queued read"},
		{name: "failed write", failed: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(subtest *testing.T) {
			subtest.Parallel()
			checkLocalWriteBarrierLimit(subtest, tc.failed)
		})
	}
}

// checkLocalWriteBarrierLimit makes the peer delete from L2 before the older fill writes.
func checkLocalWriteBarrierLimit(t *testing.T, failed bool) {
	t.Helper()
	ctx := t.Context()
	require.NotEqual(t, l2StripeIndex("n:k"), l2StripeIndex("n:z"))
	a, b, shared := newPeers(t)
	if failed {
		a.l2 = &failSetForKeyL2{l2: shared, key: "n:z"}
	}
	aView, bView := NewTyped[int](a, "n"), NewTyped[int](b, "n")
	entered, release, fillHook := blockL2WriteForKey(t, "set", "n:k", "1")
	setWritten, peerDeleted := make(chan struct{}), make(chan struct{})
	signals := map[[2]string]func(){
		{"set", "n:z"}: sync.OnceFunc(func() { close(setWritten) }),
		{"del", "n:k"}: sync.OnceFunc(func() { close(peerDeleted) }),
	}
	shared.beforeWrite = fillHook
	shared.afterWrite = func(op, key string, _ []byte) {
		if signal := signals[[2]string{op, key}]; signal != nil {
			signal()
		}
	}
	fill := startOlderPeerFill(t, release, aView)
	waitL2LockSignal(t, entered, "loader fill did not reach its L2 write")
	set := startL2LockOperation(t, release, func() error { return aView.Set(ctx, "z", 2) })
	if failed {
		waitL2LockSignal(t, set.done, "failed Set waited for the older fill")
		require.ErrorIs(t, set.err, errStub)
	} else {
		requirePeerReadsBeforeLocalBarrier(t, a, bView, setWritten, release, set)
	}
	peer := startL2LockOperation(t, release, func() error { return bView.Delete(ctx, "k") })
	waitL2LockSignal(t, peerDeleted, "peer Delete did not remove the L2 value")
	release()
	fill.requireDone(t, "loader fill did not finish after release")
	if !failed {
		set.requireDone(t, "local Set did not finish after release")
	}
	peer.requireDone(t, "peer Delete did not finish after release")
	raw, found, err := shared.Get(ctx, "n:k")
	require.NoError(t, err)
	require.True(t, found, "older fill did not recreate L2 after the peer Delete")
	require.Equal(t, "1", string(raw), "L2 did not retain the older fill after the peer Delete")
}

// requirePeerReadsBeforeLocalBarrier proves the peer can observe the Set while its barrier is queued.
func requirePeerReadsBeforeLocalBarrier(t *testing.T, a *TwoTier, peer *Typed[int], written <-chan struct{}, release func(), set *l2LockOperation) {
	t.Helper()
	waitL2LockSignal(t, written, "local Set did not publish to L2")
	requireFillGateWriter(t, &a.l2FillGate, func(bool) {
		requireOperationRunning(t, set, "local Set returned before the older fill barrier opened")
	}, "local Set never queued on the fill gate")
	read := startL2LockOperation(t, release, func() error { return readCachedL2LockTestKey(t.Context(), peer, "z", 2) })
	read.requireDone(t, "peer Get did not read the Set value while the barrier was queued")
}

type failSetForKeyL2 struct {
	l2
	key string
}

// Set fails only the configured key and delegates every other L2 write.
func (s *failSetForKeyL2) Set(ctx context.Context, key string, raw []byte, ttl time.Duration) error {
	if key == s.key {
		return fmt.Errorf("cache/twotier: test L2 set %q: %w", key, errStub)
	}
	if err := s.l2.Set(ctx, key, raw, ttl); err != nil {
		return fmt.Errorf("cache/twotier: test L2 set %q: %w", key, err)
	}
	return nil
}

// TestInvalidation_PeerDeleteWinsAfterBlockedFill covers same-stripe key, other-stripe key, and prefix notices.
func TestInvalidation_PeerDeleteWinsAfterBlockedFill(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	const otherKey = "z"
	cases := [...]struct {
		name   string
		mutate func(context.Context, *Typed[int]) error
	}{
		{name: "key", mutate: func(ctx context.Context, view *Typed[int]) error {
			return view.Set(ctx, "k", 2)
		}},
		{name: "other-stripe", mutate: func(ctx context.Context, view *Typed[int]) error {
			return view.Set(ctx, otherKey, 2)
		}},
		{name: "prefix", mutate: func(ctx context.Context, view *Typed[int]) error {
			return view.InvalidatePrefix(ctx, "k")
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(subtest *testing.T) {
			subtest.Parallel()
			if tc.name == "other-stripe" {
				require.NotEqual(subtest, l2StripeIndex("n:k"), l2StripeIndex("n:"+otherKey))
			}
			checkPeerDeleteAfterBlockedFill(subtest, tc.mutate)
		})
	}
}

// checkPeerDeleteAfterBlockedFill holds the older fill until the peer notice queues on its fill gate.
func checkPeerDeleteAfterBlockedFill(t *testing.T, mutate func(context.Context, *Typed[int]) error) {
	t.Helper()
	ctx := t.Context()
	broadcastStarted := make(chan struct{})
	a, b, shared := newPeersWithBroadcastSignal(t, sync.OnceFunc(func() { close(broadcastStarted) }))
	aView, bView := NewTyped[int](a, "n"), NewTyped[int](b, "n")
	entered, release, fillHook := blockL2WriteForKey(t, "set", "n:k", "1")
	deleteReached := make(chan struct{})
	signal := sync.OnceFunc(func() { close(deleteReached) })
	shared.beforeWrite = func(op, key string, raw []byte) {
		fillHook(op, key, raw)
		if op == "del" && key == "n:k" {
			signal()
		}
	}
	fill := startOlderPeerFill(t, release, aView)
	waitL2LockSignal(t, entered, "loader fill did not reach its L2 write")
	peer := startL2LockOperation(t, release, func() error {
		if err := mutate(ctx, bView); err != nil {
			return fmt.Errorf("cache/twotier: mutate peer before delete: %w", err)
		}
		return bView.Delete(ctx, "k")
	})
	waitL2LockSignal(t, broadcastStarted, "peer did not start its first notice while the fill was paused")
	requireFillGateWriter(t, &a.l2FillGate, func(bool) {
		requirePeerNoticeFenced(t, deleteReached, peer.done)
	}, "peer notice never queued on the receiving fill gate")
	release()
	fill.requireDone(t, "loader fill did not finish after release")
	peer.requireDone(t, "peer mutations did not finish after release")
	_, found, err := shared.Get(ctx, "n:k")
	require.NoError(t, err)
	require.False(t, found, "older fill recreated the deleted L2 entry")
	requirePeerReload(t, ctx, aView)
	// Clear the first reload from shared L2 so the second replica checks its own L1.
	require.NoError(t, shared.Del(ctx, "n:k"))
	requirePeerReload(t, ctx, bView)
}

// startOlderPeerFill runs the same loader fill for the local barrier and peer notice tests.
func startOlderPeerFill(t *testing.T, release func(), view *Typed[int]) *l2LockOperation {
	t.Helper()
	ctx := t.Context()
	return startL2LockOperation(t, release, func() error {
		value, err := view.Get(ctx, "k", func(context.Context) (int, error) { return 1, nil })
		return checkL2LockTestValue(value, err, 1)
	})
}

// requireFillGateWriter checks progress before each poll and reports when a writer is queued behind a held reader.
func requireFillGateWriter(t *testing.T, gate *sync.RWMutex, requirePending func(queued bool), timeoutMessage string) {
	t.Helper()
	// Give the worker time to queue between lock attempts.
	const fillGatePollInterval = time.Millisecond
	// Bound observations even if the worker never queues.
	const fillGatePollLimit = int(l2LockTimeout / fillGatePollInterval)
	ticks := time.NewTicker(fillGatePollInterval)
	defer ticks.Stop()
	deadline := time.NewTimer(l2LockTimeout)
	defer deadline.Stop()
poll:
	for range fillGatePollLimit {
		requirePending(false)
		if !gate.TryRLock() {
			requirePending(true)
			return
		}
		gate.RUnlock()
		select {
		case <-deadline.C:
			break poll
		case <-ticks.C:
		}
	}
	requirePending(false)
	t.Fatal(timeoutMessage)
}

// requirePeerNoticeFenced checks for peer progress while its first notice should be blocked.
func requirePeerNoticeFenced(t *testing.T, deleteReached, peerDone <-chan struct{}) {
	t.Helper()
	var earlyProgress string
	select {
	case <-deleteReached:
		earlyProgress = "peer Delete reached shared L2"
	case <-peerDone:
		earlyProgress = "peer worker returned"
	default:
	}
	require.Empty(t, earlyProgress, "missing loader-fill fence: peer progressed before fill release")
}

// requirePeerReload verifies a fresh Get reaches the origin after the peer Delete.
func requirePeerReload(t *testing.T, ctx context.Context, view *Typed[int]) {
	t.Helper()
	var loads atomic.Int32
	reload := startL2LockOperation(t, nil, func() error {
		value, err := view.Get(ctx, "k", countingLoader(&loads, 3))
		return checkL2LockTestValue(value, err, 3)
	})
	reload.requireDone(t, "Get after peer Delete did not finish")
	require.EqualValues(t, 1, loads.Load(), "Get after peer Delete must call the loader")
}

func TestInvalidation_PrefixEvictsPeerMatchesOnly(t *testing.T) {
	t.Parallel()
	a, b, _ := newPeers(t)
	peerView := NewTyped[int](b, "n")
	ctx := context.Background()
	for _, k := range []string{"user/1", "other/1"} {
		require.NoError(t, peerView.Set(ctx, k, 1))
	}
	require.NoError(t, NewTyped[int](a, "n").InvalidatePrefix(ctx, "user/"))
	var loads atomic.Int32
	for _, k := range []string{"user/1", "other/1"} {
		_, err := peerView.Get(ctx, k, countingLoader(&loads, 0))
		require.NoError(t, err)
	}
	require.EqualValues(t, 1, loads.Load(), "only user/1 left the peer's L1 and L2")
}

func TestInvalidation_IgnoresOwnNotices(t *testing.T) {
	t.Parallel()
	a, _, shared := newPeers(t)
	view := NewTyped[int](a, "n")
	require.NoError(t, view.Set(context.Background(), "k", 5))
	before := shared.getCalls.Load()
	var loads atomic.Int32
	value, err := view.Get(context.Background(), "k", countingLoader(&loads, 0))
	require.NoError(t, err)
	require.Equal(t, 5, value)
	require.Equal(t, before, shared.getCalls.Load(), "own notice must not evict the fresh L1 entry")
}

func TestInvalidation_RemoteNoticeFencesPeerLoad(t *testing.T) {
	t.Parallel()
	a, b, _ := newPeers(t)
	release, done := inFlightLoad(NewTyped[int](b, "n"))
	require.NoError(t, NewTyped[int](a, "n").Delete(context.Background(), "a"))
	close(release)
	require.NoError(t, <-done)
	var loads atomic.Int32
	_, err := NewTyped[int](b, "n").Get(context.Background(), "a", countingLoader(&loads, 9))
	require.NoError(t, err)
	require.EqualValues(t, 1, loads.Load(), "the fenced load did not populate L1")
}

func TestInvalidation_ListenStopEndsDelivery(t *testing.T) {
	t.Parallel()
	shared := newStubL2()
	broadcaster, err := NewBusBroadcaster(pubsub.New(), "", nil)
	require.NoError(t, err)
	a, b := newPeer(t, shared, broadcaster), newPeer(t, shared, broadcaster)
	b.Listen()()
	cacheOn(t, b)
	require.NoError(t, NewTyped[int](a, "n").Delete(context.Background(), "k"))
	cacheOn(t, b)
}

// failingBroadcaster fails every Broadcast, optionally after waiting for ctx.
type failingBroadcaster struct {
	wait        bool
	sawDeadline atomic.Bool
}

var errBroadcastDown = errors.New("bus down")

func (f *failingBroadcaster) Broadcast(ctx context.Context, _ Invalidation) error {
	_, ok := ctx.Deadline()
	f.sawDeadline.Store(ok)
	if f.wait {
		<-ctx.Done()
		return ctx.Err()
	}
	return errBroadcastDown
}

func (*failingBroadcaster) Subscribe(func(Invalidation)) func() { return func() {} }

func TestInvalidation_BroadcastFailureIsSurfaced(t *testing.T) {
	t.Parallel()
	shared := newStubL2()
	broadcaster := &failingBroadcaster{}
	view := NewTyped[int](newPeer(t, shared, broadcaster), "n")
	ctx := context.Background()
	mutations := []struct {
		name   string
		mutate func() error
	}{
		{"set", func() error { return view.Set(ctx, "k", 1) }},
		{"delete", func() error { return view.Delete(ctx, "k") }},
		{"prefix", func() error { return view.InvalidatePrefix(ctx, "k") }},
	}
	for _, m := range mutations {
		err := m.mutate()
		require.ErrorIs(t, err, ErrBroadcast, m.name)
		require.ErrorIs(t, err, errBroadcastDown, m.name)
	}
	require.True(t, broadcaster.sawDeadline.Load(), "broadcast runs under a deadline")
	_, found, err := shared.Get(ctx, "n:k")
	require.NoError(t, err)
	require.False(t, found, "local tiers changed before the broadcast failed")
}

func TestInvalidation_BroadcastTimeoutBounds(t *testing.T) {
	t.Parallel()
	l1, err := memory.NewForTest(100, 0)
	require.NoError(t, err)
	opts := Options{L2: L2None, L1TTL: time.Hour, Invalidation: InvalidationOptions{Enabled: true, Timeout: 10 * time.Millisecond}}
	tt, err := New(l1, opts, slog.New(slog.DiscardHandler), WithBroadcaster(&failingBroadcaster{wait: true}))
	require.NoError(t, err)
	err = NewTyped[int](tt, "n").Delete(context.Background(), "k")
	require.ErrorIs(t, err, context.DeadlineExceeded)
}

func TestBusBroadcaster_DropsMalformedNotices(t *testing.T) {
	t.Parallel()
	bus := pubsub.New()
	var logs bytes.Buffer
	broadcaster, err := NewBusBroadcaster(bus, "topic", slog.New(slog.NewTextHandler(&logs, nil)))
	require.NoError(t, err)
	var received atomic.Int32
	stop := broadcaster.Subscribe(func(Invalidation) { received.Add(1) })
	defer stop()
	for _, data := range []any{
		[]byte("not json"),
		42,
		`{"origin":"","kind":"key","key":"k"}`,
		`{"origin":"o","kind":"flush","key":"k"}`,
	} {
		bus.Publish(context.Background(), pubsub.Message{Topic: "topic", Data: data})
	}
	bus.Publish(context.Background(), pubsub.Message{Topic: "topic", Data: `{"origin":"o","kind":"prefix","key":"p"}`})
	require.EqualValues(t, 1, received.Load())
	require.Equal(t, 4, bytes.Count(logs.Bytes(), []byte("drop malformed invalidation")))
}

// plainBus hides LocalBus's CheckedBus so Broadcast takes the fire-and-forget path.
type plainBus struct{ pubsub.Bus }

func TestBusBroadcaster_PlainBusIsFireAndForget(t *testing.T) {
	t.Parallel()
	local := pubsub.New()
	var received atomic.Int32
	defer local.Subscribe(DefaultInvalidationTopic, func(pubsub.Message) { received.Add(1) })()
	broadcaster, err := NewBusBroadcaster(plainBus{Bus: local}, "", nil)
	require.NoError(t, err)
	require.NoError(t, broadcaster.Broadcast(context.Background(), Invalidation{Origin: "o", Kind: InvalidationKey, Key: "k"}))
	require.EqualValues(t, 1, received.Load())
	require.NotNil(t, broadcaster.Subscribe(nil))
}

// gapBus is a LocalBus whose test can fire the reconnect callback.
type gapBus struct {
	*pubsub.LocalBus
	onGap atomic.Pointer[func()]
}

func (g *gapBus) SubscribeWithGap(topic string, h pubsub.Handler, onGap func()) func() {
	g.onGap.Store(&onGap)
	return g.Subscribe(topic, h)
}

// TestBusBroadcaster_GapEvictsPeerL1 pins that a bus reconnect clears the
// listener's whole L1, since notices sent during the outage are lost (#642).
func TestBusBroadcaster_GapEvictsPeerL1(t *testing.T) {
	t.Parallel()
	bus := &gapBus{LocalBus: pubsub.New()}
	broadcaster, err := NewBusBroadcaster(bus, "", nil)
	require.NoError(t, err)
	peer := newPeer(t, newStubL2(), broadcaster)
	t.Cleanup(peer.Listen())
	cacheOn(t, peer)
	_, cached := peer.l1.GetIfPresent("n:k")
	require.True(t, cached, "L1 holds the value before the gap")

	onGap := bus.onGap.Load()
	require.NotNil(t, onGap, "Listen subscribes through SubscribeWithGap")
	(*onGap)()
	_, cached = peer.l1.GetIfPresent("n:k")
	require.False(t, cached, "a reconnect evicts L1")
}

func TestNewBusBroadcaster_RejectsNilBus(t *testing.T) {
	t.Parallel()
	broadcaster, err := NewBusBroadcaster(nil, "", nil)
	require.ErrorIs(t, err, errInvalidDependency)
	require.Nil(t, broadcaster)
}

func TestApplyInvalidation_IgnoresUnknownKind(t *testing.T) {
	t.Parallel()
	var logs bytes.Buffer
	tt := newL1OnlyTwoTier(t)
	tt.logger = slog.New(slog.NewTextHandler(&logs, nil))
	require.NoError(t, NewTyped[int](tt, "n").Set(context.Background(), "k", 1))
	tt.applyInvalidation(Invalidation{Origin: "peer", Kind: "flush", Key: "n:k"})
	var loads atomic.Int32
	_, err := NewTyped[int](tt, "n").Get(context.Background(), "k", countingLoader(&loads, 0))
	require.NoError(t, err)
	require.Zero(t, loads.Load())
	require.Contains(t, logs.String(), "unknown invalidation kind")
}

func TestListen_NoBroadcasterIsNoop(t *testing.T) {
	t.Parallel()
	var nilCache *TwoTier
	nilCache.Listen()()
	newL1OnlyTwoTier(t).Listen()()
}

// typedNilClient is a non-nil interface holding a nil client.
type typedNilClient struct{ rueidis.Client }

// TestNew_ValidatesModesAndInvalidation checks constructor dependencies and the selected cache mode.
func TestNew_ValidatesModesAndInvalidation(t *testing.T) {
	t.Parallel()
	l1, err := memory.NewForTest(10, 0)
	require.NoError(t, err)
	logger := slog.New(slog.DiscardHandler)
	broadcaster, err := NewBusBroadcaster(pubsub.New(), "", nil)
	require.NoError(t, err)
	enabled := InvalidationOptions{Enabled: true}
	tests := map[string]struct {
		opts    Options
		options []Option
	}{
		"unknown l2":                  {opts: Options{L2: "memcached"}},
		"redis without client":        {opts: Options{L2: L2Redis}, options: []Option{WithRedis((*typedNilClient)(nil))}},
		"none with redis client":      {opts: Options{L2: L2None}, options: []Option{WithRedis(typedNilClient{})}},
		"enabled without broadcaster": {opts: Options{L2: L2None, L1TTL: time.Minute, Invalidation: enabled}},
		"broadcaster without enabled": {opts: Options{L2: L2None, L1TTL: time.Minute}, options: []Option{WithBroadcaster(broadcaster)}},
		"enabled without L1 TTL":      {opts: Options{L2: L2None, Invalidation: enabled}, options: []Option{WithBroadcaster(broadcaster)}},
		"negative broadcast timeout":  {opts: Options{L2: L2None, L1TTL: time.Minute, Invalidation: InvalidationOptions{Enabled: true, Timeout: -time.Second}}, options: []Option{WithBroadcaster(broadcaster)}},
	}
	for name, tc := range tests {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			tt, newErr := New(l1, tc.opts, logger, tc.options...)
			require.Error(t, newErr)
			require.Nil(t, tt)
		})
	}
	tt, err := New(l1, Options{L2: L2None, L1TTL: time.Minute, Invalidation: enabled}, logger, WithBroadcaster(broadcaster), nil)
	require.NoError(t, err)
	require.True(t, tt.l1Only)
	require.Equal(t, defaultBroadcastTimeout, tt.broadcastTimeout)
	require.NotEmpty(t, tt.origin)
	redisCache, err := New(l1, Options{L2: L2Redis}, logger, WithRedis(typedNilClient{}))
	require.NoError(t, err)
	require.False(t, redisCache.l1Only)
}
