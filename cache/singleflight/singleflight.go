// SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
//
// SPDX-License-Identifier: EUPL-1.2

// Package singleflight exposes [golang.org/x/sync/singleflight] as a
// tiny typed wrapper and fx module. It de-duplicates concurrent
// identical reads so only one goroutine hits the backing store while
// the others wait and share the result.
//
// Typical use:
//
//	// Provide via fx (or construct directly):
//	g := singleflight.New[string, *User]()
//
//	// In a handler / service:
//	user, err := g.Do(ctx, userID, func(ctx context.Context) (*User, error) {
//	    return db.LoadUser(ctx, userID)
//	})
package singleflight

import (
	"context"
	"fmt"
	"runtime/debug"
	"sync"

	"golang.org/x/sync/singleflight"
)

const sharedFlightKey = "flight"

type callbackPanic struct {
	value any
	stack []byte
}

func (p *callbackPanic) Error() string {
	return fmt.Sprintf("singleflight: callback panic: %v\n%s", p.value, p.stack)
}

// invocation tracks whether a DoCancelable caller started or left before its callback ran.
type invocation struct {
	running bool
	left    bool
}

type keyedFlight struct {
	group      singleflight.Group
	users      int
	running    bool
	cancelable bool
	panicking  bool
}

// Group de-duplicates concurrent calls with the same key K.
// V is the result type. Errors are propagated to every waiter.
type Group[K comparable, V any] struct {
	mu      sync.Mutex
	flights map[K]*keyedFlight
}

// New returns an initialised Group.
func New[K comparable, V any]() *Group[K, V] {
	return &Group[K, V]{flights: make(map[K]*keyedFlight)}
}

// Do executes fn exactly once for concurrent callers sharing the same
// key. fn receives the context of the first caller — other callers'
// contexts are not forwarded (singleflight design). Callers that need
// per-call context cancellation should check ctx.Done() after Do
// returns.
func (g *Group[K, V]) Do(ctx context.Context, key K, fn func(ctx context.Context) (V, error)) (V, bool, error) {
	flight := g.acquire(key)
	defer g.release(key, flight)
	value, err, shared := flight.group.Do(sharedFlightKey, func() (any, error) {
		return g.call(ctx, key, flight, nil, fn)
	})
	if err != nil {
		var zero V
		return zero, shared, err //nolint:wrapcheck // WHY: preserve the callback error's identity.
	}
	v, ok := value.(V)
	if !ok {
		var zero V
		return zero, shared, nil
	}
	return v, shared, nil
}

// DoCancelable shares a flight while allowing each caller to stop waiting when its context ends.
// A canceled caller whose context the callback runs under waits for the callback to return before it returns; other callers return at once.
func (g *Group[K, V]) DoCancelable(ctx context.Context, key K, fn func(context.Context) (V, error)) (V, bool, error) {
	var zero V
	if err := ctx.Err(); err != nil {
		return zero, false, fmt.Errorf("singleflight: wait: %w", err)
	}
	self := &invocation{}
	flight, done := g.start(ctx, key, self, fn)
	defer g.release(key, flight)
	select {
	case <-ctx.Done():
		if g.leave(self) {
			<-done
		}
		return zero, false, fmt.Errorf("singleflight: wait: %w", ctx.Err())
	case result := <-done:
		if err := ctx.Err(); err != nil {
			return zero, false, fmt.Errorf("singleflight: wait: %w", err)
		}
		return flightValue[V](result)
	}
}

// Forget evicts the in-flight or cached result for key, so the next
// caller will execute fn again.
func (g *Group[K, V]) Forget(key K) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if flight := g.flights[key]; flight != nil {
		delete(g.flights, key)
		flight.group.Forget(sharedFlightKey)
	}
}

func (g *Group[K, V]) start(ctx context.Context, key K, self *invocation, fn func(context.Context) (V, error)) (*keyedFlight, <-chan singleflight.Result) {
	g.mu.Lock()
	defer g.mu.Unlock()
	flight := g.acquireLocked(key)
	flight.cancelable = true
	return flight, flight.group.DoChan(sharedFlightKey, func() (any, error) {
		return g.call(ctx, key, flight, self, fn)
	})
}

// leave marks a DoCancelable caller gone and reports whether its callback may still be running.
func (g *Group[K, V]) leave(self *invocation) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	self.left = !self.running
	return self.running
}

// begin admits a flight callback or rejects a caller that left before the callback started.
func (g *Group[K, V]) begin(ctx context.Context, key K, flight *keyedFlight, self *invocation) error {
	g.mu.Lock()
	defer g.mu.Unlock()
	if self != nil && self.left {
		return fmt.Errorf("singleflight: call: %w", ctx.Err())
	}
	if g.flights[key] != flight && flight.users == 0 {
		return fmt.Errorf("singleflight: call: %w", context.Canceled)
	}
	if self != nil {
		self.running = true
	}
	flight.running = true
	return nil
}

func (g *Group[K, V]) acquire(key K) *keyedFlight {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.acquireLocked(key)
}

func (g *Group[K, V]) acquireLocked(key K) *keyedFlight {
	if g.flights == nil {
		g.flights = make(map[K]*keyedFlight)
	}
	flight := g.flights[key]
	if flight == nil || flight.panicking {
		flight = &keyedFlight{}
		g.flights[key] = flight
	}
	flight.users++
	return flight
}

func (g *Group[K, V]) call(ctx context.Context, key K, flight *keyedFlight, self *invocation, fn func(context.Context) (V, error)) (value any, err error) {
	if beginErr := g.begin(ctx, key, flight, self); beginErr != nil {
		return nil, beginErr
	}
	defer g.finish(key, flight)
	returned := false
	defer func() {
		if returned {
			return
		}
		g.mu.Lock()
		cancelable := flight.cancelable
		flight.panicking = !cancelable
		g.mu.Unlock()
		if cancelable {
			if recovered := recover(); recovered != nil {
				value, err = nil, &callbackPanic{value: recovered, stack: debug.Stack()}
			}
		}
	}()
	value, err = fn(ctx)
	returned = true
	return value, err
}

func (g *Group[K, V]) finish(key K, flight *keyedFlight) {
	g.mu.Lock()
	defer g.mu.Unlock()
	flight.running = false
	if flight.users == 0 && g.flights[key] == flight {
		delete(g.flights, key)
	}
}

func (g *Group[K, V]) release(key K, flight *keyedFlight) {
	g.mu.Lock()
	defer g.mu.Unlock()
	flight.users--
	if flight.users == 0 && !flight.running && g.flights[key] == flight {
		delete(g.flights, key)
	}
}

func flightValue[V any](result singleflight.Result) (V, bool, error) {
	if result.Err != nil {
		var zero V
		return zero, result.Shared, result.Err
	}
	v, ok := result.Val.(V)
	if !ok {
		var zero V
		return zero, result.Shared, nil
	}
	return v, result.Shared, nil
}
