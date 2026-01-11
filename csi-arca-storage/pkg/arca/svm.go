package arca

import (
	"context"
	"errors"
	"fmt"
	"time"

	"k8s.io/klog/v2"

	"github.com/akam1o/csi-arca-storage/pkg/lock"
)

// SVMManager manages SVM lifecycle operations
type SVMManager struct {
	client    *Client
	allocator *StandaloneAllocator
	lockMgr   *lock.Manager
	mtu       int
}

// NewSVMManager creates a new SVM manager
func NewSVMManager(client *Client, allocator *StandaloneAllocator, lockMgr *lock.Manager, mtu int) *SVMManager {
	if mtu == 0 {
		mtu = 1500 // Default MTU
	}

	return &SVMManager{
		client:    client,
		allocator: allocator,
		lockMgr:   lockMgr,
		mtu:       mtu,
	}
}

// EnsureSVM ensures an SVM exists for the given namespace (idempotent)
func (m *SVMManager) EnsureSVM(ctx context.Context, namespace string) (*SVM, error) {
	svmName := fmt.Sprintf("k8s-%s", namespace)

	// Try to get existing SVM first (fast path)
	svm, err := m.client.GetSVM(ctx, svmName)
	if err == nil {
		klog.V(4).Infof("SVM %s already exists (VIP: %s)", svmName, svm.VIP)
		return svm, nil
	}

	if err != nil && err != ErrSVMNotFound {
		return nil, fmt.Errorf("failed to check existing SVM: %w", err)
	}

	// SVM doesn't exist - need to create it with lock
	return m.createSVMWithLock(ctx, namespace, svmName)
}

// createSVMWithLock creates an SVM with distributed locking
func (m *SVMManager) createSVMWithLock(ctx context.Context, namespace, svmName string) (*SVM, error) {
	// Acquire distributed lock to prevent concurrent creation
	lockCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()

	lockHandle, err := m.lockMgr.AcquireLock(lockCtx, namespace, 30*time.Second)
	if err != nil {
		return nil, fmt.Errorf("failed to acquire lock for namespace %s: %w", namespace, err)
	}
	defer func() {
		if err := lockHandle.Release(ctx); err != nil {
			klog.Warningf("Failed to release lock for namespace %s: %v", namespace, err)
		}
	}()

	// Double-check after acquiring lock
	svm, err := m.client.GetSVM(ctx, svmName)
	if err == nil {
		klog.V(4).Infof("SVM %s was created by another controller", svmName)
		return svm, nil
	}

	if err != nil && err != ErrSVMNotFound {
		return nil, fmt.Errorf("failed to check existing SVM after lock: %w", err)
	}

	// Create SVM with retry on IP conflict
	maxAttempts := 5
	for attempt := 0; attempt < maxAttempts; attempt++ {
		if attempt > 0 {
			klog.V(4).Infof("Retrying SVM creation for namespace %s (attempt %d/%d)", namespace, attempt+1, maxAttempts)
		}

		// Allocate network resources
		netAlloc, err := m.allocator.Allocate(ctx, namespace, attempt)
		if err != nil {
			return nil, fmt.Errorf("failed to allocate network for namespace %s: %w", namespace, err)
		}

		// Create SVM request
		req := &CreateSVMRequest{
			Name:    svmName,
			VLANID:  netAlloc.VLANID,
			IPCIDR:  netAlloc.IPCIDR,
			Gateway: netAlloc.Gateway,
			MTU:     m.mtu,
		}

		// Try to create SVM
		svm, err = m.client.CreateSVM(ctx, req)
		if err == nil {
			klog.Infof("Created SVM %s for namespace %s (VIP: %s, VLAN: %d)",
				svmName, namespace, svm.VIP, svm.VLANID)
			return svm, nil
		}

		// Check error type
		if errors.Is(err, ErrSVMAlreadyExists) {
			// Another controller created it concurrently
			svm, getErr := m.client.GetSVM(ctx, svmName)
			if getErr == nil {
				return svm, nil
			}
			return nil, fmt.Errorf("svm exists but cannot retrieve: %w", getErr)
		}

		if !errors.Is(err, ErrNetworkConflict) {
			// Non-retryable error
			return nil, fmt.Errorf("failed to create SVM: %w", err)
		}

		// Network conflict - retry with different IP
		klog.V(4).Infof("Network conflict for namespace %s, retrying with different IP", namespace)
		backoff := time.Duration(1<<uint(attempt)) * time.Second
		select {
		case <-time.After(backoff):
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}

	return nil, fmt.Errorf("failed to create SVM for namespace %s after %d attempts", namespace, maxAttempts)
}

// DeleteSVM deletes an SVM (idempotent)
func (m *SVMManager) DeleteSVM(ctx context.Context, svmName string) error {
	err := m.client.DeleteSVM(ctx, svmName)
	if err != nil {
		return fmt.Errorf("failed to delete SVM %s: %w", svmName, err)
	}

	klog.Infof("Deleted SVM %s", svmName)
	return nil
}

// GetSVM retrieves SVM information
func (m *SVMManager) GetSVM(ctx context.Context, svmName string) (*SVM, error) {
	return m.client.GetSVM(ctx, svmName)
}

// GetSVMForNamespace retrieves SVM for a given namespace
func (m *SVMManager) GetSVMForNamespace(ctx context.Context, namespace string) (*SVM, error) {
	svmName := fmt.Sprintf("k8s-%s", namespace)
	return m.client.GetSVM(ctx, svmName)
}
