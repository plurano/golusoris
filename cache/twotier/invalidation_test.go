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
	shared = newStubL2()
	broadcaster, err := NewBusBroadcaster(pubsub.New(), "", nil)
	require.NoError(t, err)
	var peerBroadcaster Broadcaster = broadcaster
	if signal != nil {
		peerBroadcaster = &signalingBroadcaster{Broadcaster: broadcaster, signal: signal}
	}
	a, b = newPeer(t, shared, broadcaster), newPeer(t, shared, peerBroadcaster)
	t.Cleanup(a.Listen())
	t.Cleanup(b.Listen())
	return a, b, shared
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

// checkPeerDeleteAfterBlockedFill holds the older fill until the peer notice has had a chance to block.
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
	fill := startL2LockOperation(t, release, func() error {
		value, err := aView.Get(ctx, "k", func(context.Context) (int, error) { return 1, nil })
		return checkL2LockTestValue(value, err, 1)
	})
	waitL2LockSignal(t, entered, "loader fill did not reach its L2 write")
	peer := startL2LockOperation(t, release, func() error {
		if err := mutate(ctx, bView); err != nil {
			return fmt.Errorf("cache/twotier: mutate peer before delete: %w", err)
		}
		return bView.Delete(ctx, "k")
	})
	waitL2LockSignal(t, broadcastStarted, "peer did not start its first notice while the fill was paused")
	requirePeerNoticeFenced(t, deleteReached, peer.done)
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

// requirePeerNoticeFenced checks for peer progress while its first notice should be blocked.
func requirePeerNoticeFenced(t *testing.T, deleteReached, peerDone <-chan struct{}) {
	t.Helper()
	// Start the window at broadcast entry so an unfenced peer can reach L2 before fill release.
	const peerDeleteWindow = time.Second
	var earlyProgress string
	select {
	case <-deleteReached:
		earlyProgress = "peer Delete reached shared L2"
	case <-peerDone:
		earlyProgress = "peer worker returned"
	case <-time.After(peerDeleteWindow):
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
