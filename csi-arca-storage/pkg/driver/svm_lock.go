package driver

import (
	"context"
	"fmt"
	"time"

	"k8s.io/klog/v2"
)

const controllerSVMLockTTL = 30 * time.Second

type controllerSVMLock struct {
	token    chan struct{}
	refCount int
}

func (d *Driver) acquireControllerSVMLock(ctx context.Context, svmName string) (func(), error) {
	releaseLocalLock, err := d.acquireLocalControllerSVMLock(ctx, svmName)
	if err != nil {
		return nil, err
	}

	if d.lockManager == nil {
		return releaseLocalLock, nil
	}

	distributedLock, err := d.lockManager.AcquireLock(ctx, "controller-svm-"+svmName, controllerSVMLockTTL)
	if err != nil {
		releaseLocalLock()
		return nil, fmt.Errorf("failed to acquire distributed SVM lifecycle lock: %w", err)
	}

	return func() {
		releaseCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := distributedLock.Release(releaseCtx); err != nil {
			klog.Warningf("Failed to release distributed SVM lifecycle lock for %s: %v", svmName, err)
		}
		releaseLocalLock()
	}, nil
}

func (d *Driver) acquireLocalControllerSVMLock(ctx context.Context, svmName string) (func(), error) {
	d.controllerSVMLocksMu.Lock()
	if d.controllerSVMLocks == nil {
		d.controllerSVMLocks = make(map[string]*controllerSVMLock)
	}
	lock, ok := d.controllerSVMLocks[svmName]
	if !ok {
		lock = &controllerSVMLock{token: make(chan struct{}, 1)}
		lock.token <- struct{}{}
		d.controllerSVMLocks[svmName] = lock
	}
	lock.refCount++
	d.controllerSVMLocksMu.Unlock()

	if err := ctx.Err(); err != nil {
		d.releaseControllerSVMLockRef(svmName, lock)
		return nil, err
	}

	select {
	case <-lock.token:
		return func() {
			lock.token <- struct{}{}
			d.releaseControllerSVMLockRef(svmName, lock)
		}, nil
	case <-ctx.Done():
		d.releaseControllerSVMLockRef(svmName, lock)
		return nil, ctx.Err()
	}
}

func (d *Driver) releaseControllerSVMLockRef(svmName string, lock *controllerSVMLock) {
	d.controllerSVMLocksMu.Lock()
	defer d.controllerSVMLocksMu.Unlock()

	lock.refCount--
	if lock.refCount == 0 {
		delete(d.controllerSVMLocks, svmName)
	}
}
