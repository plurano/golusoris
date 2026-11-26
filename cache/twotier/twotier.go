// SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
//
// SPDX-License-Identifier: EUPL-1.2

// Package twotier composes the L1 in-process cache (cache/memory, otter) and
// the L2 distributed cache (cache/redis, rueidis) into a single read-through,
// write-through cache with singleflight de-duplication.
//
// A read goes L1 → L2 → loader: a hit in a faster tier short-circuits and
// back-fills the tiers it skipped. A write fans out to both tiers. Concurrent
// loads of the same key are coalesced so the loader runs once.
//
// Values cross the L1/L2 boundary as JSON: L1 stores the live Go value, L2
// stores its JSON encoding (Redis is a byte store, and JSON keeps the cache
// language-agnostic across replicas).
//
// Bulk invalidation: InvalidatePrefix evicts every entry under a key prefix
// from both tiers (otter is scanned via Keys(); Redis via SCAN MATCH + UNLINK).
//
// L1-only mode: cache.twotier.l2 = none drops the distributed tier; the same
// typed API and singleflight serve a single process without Redis.
//
// Cross-replica invalidation: with a [Broadcaster], every Set, Delete and
// InvalidatePrefix sends one notice and peers evict that key or prefix from
// their L1. Delivery is at most once, so a lost notice leaves a peer stale
// until its L1 TTL; invalidation therefore requires a positive L1 TTL.
//
// Disabled / nil-passthrough mode: a nil *Cache is a valid no-op cache. Every
// method is nil-safe — Get falls straight through to the loader, Set/Delete and
// InvalidatePrefix do nothing — so call sites never branch on whether caching
// is configured.
//
// Usage:
//
//	fx.New(
//	    golusoris.Core,
//	    golusoris.CacheMemory, // *memory.Cache (L1)
//	    golusoris.CacheRedis,  // rueidis.Client (L2)
//	    golusoris.CacheTwoTier // *twotier.TwoTier
//	)
//
//	// Get a typed view and read through it:
//	users := twotier.Typed[*User](tt, "user")
//	u, err := users.Get(ctx, id, func(ctx context.Context) (*User, error) {
//	    return db.LoadUser(ctx, id)
//	})
//
// Config key prefix: cache.twotier.*
package twotier

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/golusoris/golusoris/cache/memory"
	"github.com/golusoris/golusoris/cache/singleflight"
)

// l2 is the minimal distributed-tier contract twotier needs. The rueidis
// adapter ([redisL2]) implements it in production; tests supply an in-memory
// stub. Keeping the surface this small is what makes the package hermetically
// testable without the full rueidis.Client interface.
type l2 interface {
	// Get returns the raw bytes for key and true, (nil, false) on miss.
	Get(ctx context.Context, key string) ([]byte, bool, error)
	// Set stores raw bytes for key with the given TTL (0 = no expiry).
	Set(ctx context.Context, key string, val []byte, ttl time.Duration) error
	// Del removes key.
	Del(ctx context.Context, key string) error
	// DelPrefix removes every key whose name starts with prefix. The redis
	// adapter implements this with a cursor-paged SCAN MATCH "<prefix>*" +
	// batched UNLINK; the test stub iterates its map.
	DelPrefix(ctx context.Context, prefix string) error
}

// Loader fetches a value from the origin on a full cache miss.
type Loader[V any] func(ctx context.Context) (V, error)

type flightKey struct {
	key   string
	epoch uint64
}

const l2StripeCount = 64

// pendingSet records a Set without allocation because stripe locks admit one Set per stripe.
type pendingSet struct {
	key           string
	active, dirty bool
}

// TwoTier is the untyped two-tier cache. It is created by the fx module and
// shared across the app; callers obtain a type-safe view via [Typed].
//
// A nil *TwoTier is a valid disabled cache (see package doc).
type TwoTier struct {
	l1               *memory.Cache
	l2               l2
	l1Only           bool
	logger           *slog.Logger
	l1TTL            time.Duration
	l2TTL            time.Duration
	l2Order          [l2StripeCount]sync.Mutex
	l2Prefix         sync.RWMutex
	mutation         sync.RWMutex
	epoch            uint64
	l2Pending        [l2StripeCount]pendingSet
	group            *singleflight.Group[flightKey, []byte]
	origin           string
	broadcaster      Broadcaster
	broadcastTimeout time.Duration
}

// Typed is a type-safe view over a [TwoTier] with a key prefix. Multiple typed
// views can share one TwoTier without colliding. V is the stored value type.
//
// A view built from a nil *TwoTier is itself a disabled no-op view.
type Typed[V any] struct {
	tt     *TwoTier
	prefix string
}

// NewTyped returns a typed view over tt with the given key prefix. tt may be
// nil, in which case the view is a no-op (Get always calls the loader).
func NewTyped[V any](tt *TwoTier, prefix string) *Typed[V] {
	return &Typed[V]{tt: tt, prefix: prefix}
}

func (t *Typed[V]) key(k string) string {
	return t.prefix + ":" + k
}

// Get returns the value for k, reading through L1 → L2 → loader. The first
// tier that has it wins and back-fills the faster tiers it skipped. Concurrent
// Gets for the same key share a single loader invocation.
//
// On a disabled (nil) cache, Get calls loader directly and caches nothing.
func (t *Typed[V]) Get(ctx context.Context, k string, loader Loader[V]) (V, error) {
	if t.tt == nil {
		return loader(ctx)
	}
	key := t.key(k)

	epoch, cached, ok := t.l1Snapshot(key)
	if ok {
		return cached, nil
	}

	flight := flightKey{key: key, epoch: epoch}
	raw, _, err := t.tt.group.Do(ctx, flight, func(ctx context.Context) ([]byte, error) {
		return t.load(ctx, key, epoch, loader)
	})
	if err != nil {
		var zero V
		return zero, fmt.Errorf("cache/twotier: get %q: %w", key, err)
	}

	var v V
	if uerr := json.Unmarshal(raw, &v); uerr != nil {
		var zero V
		return zero, fmt.Errorf("cache/twotier: decode loaded value: %w", uerr)
	}
	t.setL1IfCurrent(key, v, epoch)
	return v, nil
}

// l1Snapshot reads one generation and its typed L1 value atomically against
// Set, Delete, and prefix invalidation.
func (t *Typed[V]) l1Snapshot(key string) (uint64, V, bool) {
	t.tt.mutation.RLock()
	defer t.tt.mutation.RUnlock()
	raw, ok := t.tt.l1.GetIfPresent(key)
	if !ok {
		var zero V
		return t.tt.epoch, zero, false
	}
	v, ok := raw.(V)
	return t.tt.epoch, v, ok
}

func (t *Typed[V]) setL1(key string, value V) {
	t.tt.l1.Set(key, value)
	if t.tt.l1TTL > 0 {
		t.tt.l1.SetExpiresAfter(key, t.tt.l1TTL)
	}
}

// setL1IfCurrent prevents a pre-Set L2 read from overwriting a value another Get returned.
func (t *Typed[V]) setL1IfCurrent(key string, value V, epoch uint64) {
	t.tt.mutation.Lock()
	defer t.tt.mutation.Unlock()
	pending := t.tt.l2Pending[l2StripeIndex(key)]
	if t.tt.epoch == epoch && (!pending.active || pending.key != key) {
		t.setL1(key, value)
	}
}

// load runs inside singleflight: it checks L2, then falls back to the loader,
// populating L2 on an origin hit. It returns the JSON bytes so L1 can be
// back-filled by the caller after the assertion succeeds.
func (t *Typed[V]) load(ctx context.Context, key string, epoch uint64, loader Loader[V]) ([]byte, error) {
	if raw, ok, err := t.tt.l2.Get(ctx, key); err != nil {
		t.tt.logger.WarnContext(ctx, "cache/twotier: L2 get failed, falling through", slog.Any("error", err))
	} else if ok {
		var cached V
		if decodeErr := json.Unmarshal(raw, &cached); decodeErr == nil {
			return raw, nil
		}
		t.tt.logger.WarnContext(ctx, "cache/twotier: corrupt L2 value, falling through",
			slog.String("key", key))
		if deleteErr := t.tt.deleteL2IfCurrent(ctx, key, epoch); deleteErr != nil {
			t.tt.logger.WarnContext(ctx, "cache/twotier: delete corrupt L2 value",
				slog.String("key", key), slog.Any("error", deleteErr))
		}
	}

	v, err := loader(ctx)
	if err != nil {
		return nil, fmt.Errorf("loader: %w", err)
	}
	raw, err := json.Marshal(v)
	if err != nil {
		return nil, fmt.Errorf("cache/twotier: encode loaded value: %w", err)
	}
	if serr := t.tt.setL2IfCurrent(ctx, key, raw, epoch); serr != nil {
		t.tt.logger.WarnContext(ctx, "cache/twotier: L2 set failed", slog.Any("error", serr))
	}
	return raw, nil
}

// l2StripeIndex uses FNV-1a over the composed key to select a stripe without allocating.
func l2StripeIndex(key string) int {
	const offset64 uint64 = 14695981039346656037
	const prime64 uint64 = 1099511628211
	hash := offset64
	for i := range len(key) {
		hash ^= uint64(key[i])
		hash *= prime64
	}
	return int(hash % l2StripeCount)
}

// lockL2Key keeps each key's L2 writes ordered with same-key writes and prefix deletes.
func (t *TwoTier) lockL2Key(key string) func() {
	t.l2Prefix.RLock()
	stripe := &t.l2Order[l2StripeIndex(key)]
	stripe.Lock()
	return func() {
		stripe.Unlock()
		t.l2Prefix.RUnlock()
	}
}

// isCurrentEpoch checks the fence without holding mutation during L2 I/O.
func (t *TwoTier) isCurrentEpoch(epoch uint64) bool {
	t.mutation.RLock()
	defer t.mutation.RUnlock()
	return t.epoch == epoch
}

// setL2IfCurrent fences loader fills superseded before they acquire the write locks.
func (t *TwoTier) setL2IfCurrent(ctx context.Context, key string, raw []byte, epoch uint64) error {
	unlock := t.lockL2Key(key)
	defer unlock()
	if !t.isCurrentEpoch(epoch) {
		return nil
	}
	if err := t.l2.Set(ctx, key, raw, t.l2TTL); err != nil {
		return fmt.Errorf("cache/twotier: set current L2 value: %w", err)
	}
	return nil
}

// deleteL2IfCurrent prevents corruption repair from deleting a superseding write.
func (t *TwoTier) deleteL2IfCurrent(ctx context.Context, key string, epoch uint64) error {
	unlock := t.lockL2Key(key)
	defer unlock()
	if !t.isCurrentEpoch(epoch) {
		return nil
	}
	if err := t.l2.Del(ctx, key); err != nil {
		return fmt.Errorf("cache/twotier: delete current L2 value: %w", err)
	}
	return nil
}

// Set writes v to L2 and tells peers to evict k. An L1-only cache always
// publishes v to L1. With shared L2, Set publishes v to L1 unless a peer notice
// for k or a prefix covering k arrives during the write. A disabled cache is a
// no-op. An error wrapping [ErrBroadcast] means the local write succeeded but
// peers were not told.
func (t *Typed[V]) Set(ctx context.Context, k string, v V) error {
	if t.tt == nil {
		return nil
	}
	key := t.key(k)
	raw, err := json.Marshal(v)
	if err != nil {
		return fmt.Errorf("cache/twotier: encode value: %w", err)
	}
	if err := t.setBoth(ctx, key, raw, v); err != nil {
		return err
	}
	return t.tt.broadcast(ctx, InvalidationKey, key)
}

// setBoth advances the epoch and updates L1 only after L2 accepts the write.
func (t *Typed[V]) setBoth(ctx context.Context, key string, raw []byte, value V) error {
	unlock := t.tt.lockL2Key(key)
	defer unlock()
	stripe := l2StripeIndex(key)
	t.tt.mutation.Lock()
	t.tt.l2Pending[stripe] = pendingSet{key: key, active: true}
	t.tt.mutation.Unlock()
	err := t.tt.l2.Set(ctx, key, raw, t.tt.l2TTL)
	t.tt.mutation.Lock()
	defer t.tt.mutation.Unlock()
	pending := t.tt.l2Pending[stripe]
	t.tt.l2Pending[stripe] = pendingSet{}
	if err != nil {
		return fmt.Errorf("cache/twotier: L2 set: %w", err)
	}
	t.tt.epoch++
	// L1-only caches have no newer L2 value to read after a peer notice.
	if t.tt.l1Only || !pending.dirty {
		t.setL1(key, value)
	} else {
		t.tt.l1.Invalidate(key)
	}
	return nil
}

// Delete removes k from both tiers and tells peers to evict it. On a disabled
// cache it is a no-op. An error wrapping [ErrBroadcast] means k is gone
// locally but peers were not told.
func (t *Typed[V]) Delete(ctx context.Context, k string) error {
	if t.tt == nil {
		return nil
	}
	key := t.key(k)
	if err := t.tt.deleteBoth(ctx, key); err != nil {
		return err
	}
	return t.tt.broadcast(ctx, InvalidationKey, key)
}

// deleteBoth preserves the L1 entry and epoch if L2 deletion fails.
func (t *TwoTier) deleteBoth(ctx context.Context, key string) error {
	unlock := t.lockL2Key(key)
	defer unlock()
	if derr := t.l2.Del(ctx, key); derr != nil {
		return fmt.Errorf("cache/twotier: L2 delete: %w", derr)
	}
	t.mutation.Lock()
	defer t.mutation.Unlock()
	t.epoch++
	t.l1.Invalidate(key)
	return nil
}

// InvalidatePrefix evicts every entry under this view whose user key starts
// with prefix, from both tiers. The view's own key prefix is composed in
// exactly as Get/Set/Delete do (t.key), so passing "" clears the whole view.
//
// L1 has no native prefix delete: otter is scanned via Keys() and matching
// entries are Invalidated one by one. L2 is cleared by the adapter's prefix
// delete (SCAN MATCH + UNLINK on Redis). On a disabled cache it is a no-op.
func (t *Typed[V]) InvalidatePrefix(ctx context.Context, prefix string) error {
	if t.tt == nil {
		return nil
	}
	return t.tt.InvalidatePrefix(ctx, t.key(prefix))
}

// InvalidatePrefix evicts every entry whose composed key starts with prefix,
// from both tiers. It takes already-composed keys (no view prefix is added);
// the typed [Typed.InvalidatePrefix] is the per-view wrapper. A nil *TwoTier
// is a no-op.
//
// L1 (otter) is scanned via Keys() because it has no native prefix delete;
// matching keys are Invalidated individually and forgotten from singleflight.
// L2 prefix eviction is delegated to the [l2] adapter. Peers are told to
// evict the prefix; an error wrapping [ErrBroadcast] means they were not.
func (t *TwoTier) InvalidatePrefix(ctx context.Context, prefix string) error {
	if t == nil {
		return nil
	}
	if err := t.invalidatePrefixBoth(ctx, prefix); err != nil {
		return err
	}
	return t.broadcast(ctx, InvalidationPrefix, prefix)
}

// invalidatePrefixBoth excludes key writers until both tiers and the epoch reflect the deletion.
func (t *TwoTier) invalidatePrefixBoth(ctx context.Context, prefix string) error {
	t.l2Prefix.Lock()
	defer t.l2Prefix.Unlock()
	if derr := t.l2.DelPrefix(ctx, prefix); derr != nil {
		return fmt.Errorf("cache/twotier: L2 invalidate prefix %q: %w", prefix, derr)
	}
	t.mutation.Lock()
	defer t.mutation.Unlock()
	t.epoch++
	t.evictL1Prefix(prefix)
	return nil
}

// evictL1Prefix drops every string L1 key under prefix; callers hold mutation.
func (t *TwoTier) evictL1Prefix(prefix string) {
	for rawKey := range t.l1.Keys() {
		key, ok := rawKey.(string)
		if !ok {
			continue
		}
		if strings.HasPrefix(key, prefix) {
			t.l1.Invalidate(key)
		}
	}
}

// broadcast tells peers about one local mutation, bounded by broadcastTimeout.
func (t *TwoTier) broadcast(ctx context.Context, kind InvalidationKind, key string) error {
	if t.broadcaster == nil {
		return nil
	}
	sendCtx, cancel := context.WithTimeout(ctx, t.broadcastTimeout)
	defer cancel()
	if err := t.broadcaster.Broadcast(sendCtx, Invalidation{Origin: t.origin, Kind: kind, Key: key}); err != nil {
		return fmt.Errorf("%w: %s %q: %w", ErrBroadcast, kind, key, err)
	}
	return nil
}

// Listen subscribes to peer invalidations and returns the func that stops
// it. Without a [Broadcaster], or on a nil *TwoTier, it is a no-op. The fx
// module calls it on start.
func (t *TwoTier) Listen() (stop func()) {
	if t == nil || t.broadcaster == nil {
		return func() {}
	}
	return t.broadcaster.Subscribe(t.applyInvalidation)
}

// applyInvalidation evicts a peer's mutation from L1 and fences in-flight
// loads; L2 already holds the peer's write.
func (t *TwoTier) applyInvalidation(inv Invalidation) {
	if inv.Origin == t.origin {
		return
	}
	t.mutation.Lock()
	defer t.mutation.Unlock()
	switch inv.Kind {
	case InvalidationKey:
		t.epoch++
		pending := &t.l2Pending[l2StripeIndex(inv.Key)]
		if pending.active && pending.key == inv.Key {
			pending.dirty = true
		}
		t.l1.Invalidate(inv.Key)
	case InvalidationPrefix:
		t.epoch++
		for i := range l2StripeCount {
			pending := &t.l2Pending[i]
			if pending.active && strings.HasPrefix(pending.key, inv.Key) {
				pending.dirty = true
			}
		}
		t.evictL1Prefix(inv.Key)
	default:
		t.logger.Warn("cache/twotier: ignore unknown invalidation kind", slog.String("kind", string(inv.Kind)))
	}
}
