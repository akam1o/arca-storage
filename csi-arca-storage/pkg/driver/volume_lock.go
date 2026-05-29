package driver

import (
	"context"
	"fmt"
	"time"

	"k8s.io/klog/v2"
)

const (
	volumeLifecycleLockTTL            = 30 * time.Second
	volumeLifecycleLockResourcePrefix = "volume-create-"
)

type volumeCreateLock struct {
	token    chan struct{}
	refCount int
}

type lifecycleLockContextKey struct{}

type lifecycleLockState interface {
	Done() <-chan struct{}
	Err() error
}

func withLifecycleLock(ctx context.Context, lockState lifecycleLockState) context.Context {
	return context.WithValue(ctx, lifecycleLockContextKey{}, lockState)
}

func lifecycleLockFromContext(ctx context.Context) lifecycleLockState {
	lockState, _ := ctx.Value(lifecycleLockContextKey{}).(lifecycleLockState)
	return lockState
}

func (d *Driver) acquireVolumeCreateLock(ctx context.Context, volumeID string) (context.Context, func(), error) {
	return d.acquireVolumeLifecycleLock(ctx, volumeID, "create")
}

func (d *Driver) acquireVolumeLifecycleLock(ctx context.Context, volumeID, operation string) (context.Context, func(), error) {
	releaseLocalLock, err := d.acquireLocalVolumeCreateLock(ctx, volumeID)
	if err != nil {
		return ctx, nil, err
	}

	if d.lockManager == nil {
		return ctx, releaseLocalLock, nil
	}

	distributedLock, err := d.lockManager.AcquireLock(
		ctx,
		volumeLifecycleLockResourcePrefix+volumeID,
		volumeLifecycleLockTTL,
	)
	if err != nil {
		releaseLocalLock()
		return ctx, nil, fmt.Errorf("failed to acquire distributed volume %s lock: %w", operation, err)
	}

	lockedCtx, cancelLockedCtx := distributedLock.Context(ctx)
	lockedCtx = withLifecycleLock(lockedCtx, distributedLock)
	return lockedCtx, func() {
		cancelLockedCtx()
		releaseCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := distributedLock.Release(releaseCtx); err != nil {
			klog.Warningf("Failed to release distributed volume %s lock for %s: %v", operation, volumeID, err)
		}
		releaseLocalLock()
	}, nil
}

func (d *Driver) acquireLocalVolumeCreateLock(ctx context.Context, volumeID string) (func(), error) {
	d.volumeCreateLocksMu.Lock()
	if d.volumeCreateLocks == nil {
		d.volumeCreateLocks = make(map[string]*volumeCreateLock)
	}
	lock, ok := d.volumeCreateLocks[volumeID]
	if !ok {
		lock = &volumeCreateLock{token: make(chan struct{}, 1)}
		lock.token <- struct{}{}
		d.volumeCreateLocks[volumeID] = lock
	}
	lock.refCount++
	d.volumeCreateLocksMu.Unlock()

	if err := ctx.Err(); err != nil {
		d.releaseVolumeCreateLockRef(volumeID, lock)
		return nil, err
	}

	select {
	case <-lock.token:
		return func() {
			lock.token <- struct{}{}
			d.releaseVolumeCreateLockRef(volumeID, lock)
		}, nil
	case <-ctx.Done():
		d.releaseVolumeCreateLockRef(volumeID, lock)
		return nil, ctx.Err()
	}
}

func (d *Driver) releaseVolumeCreateLockRef(volumeID string, lock *volumeCreateLock) {
	d.volumeCreateLocksMu.Lock()
	defer d.volumeCreateLocksMu.Unlock()

	lock.refCount--
	if lock.refCount == 0 {
		delete(d.volumeCreateLocks, volumeID)
	}
}
