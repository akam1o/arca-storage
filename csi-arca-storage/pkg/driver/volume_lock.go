package driver

import "context"

type volumeCreateLock struct {
	token    chan struct{}
	refCount int
}

func (d *Driver) acquireVolumeCreateLock(ctx context.Context, volumeID string) (func(), error) {
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
