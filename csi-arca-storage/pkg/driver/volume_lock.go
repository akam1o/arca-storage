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

func (d *Driver) acquireVolumeCreateLock(ctx context.Context, volumeID string) (func(), error) {
	return d.acquireVolumeLifecycleLock(ctx, volumeID, "create")
}

func (d *Driver) acquireVolumeLifecycleLock(ctx context.Context, volumeID, operation string) (func(), error) {
	releaseLocalLock, err := d.acquireLocalVolumeCreateLock(ctx, volumeID)
	if err != nil {
		return nil, err
	}

	if d.lockManager == nil {
		return releaseLocalLock, nil
	}

	distributedLock, err := d.lockManager.AcquireLock(
		ctx,
		volumeLifecycleLockResourcePrefix+volumeID,
		volumeLifecycleLockTTL,
	)
	if err != nil {
		releaseLocalLock()
		return nil, fmt.Errorf("failed to acquire distributed volume %s lock: %w", operation, err)
	}

	return func() {
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
