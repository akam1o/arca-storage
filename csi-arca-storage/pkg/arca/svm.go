package arca

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
	"time"

	"k8s.io/klog/v2"

	"github.com/akam1o/csi-arca-storage/pkg/lock"
)

const (
	svmNamePrefix       = "k8s-"
	maxArcaSVMNameBytes = 63
	svmNameHashBytes    = 6
)

var svmReadyPollInterval = time.Second

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
	svmName := svmNameForNamespace(namespace)

	// Try to get existing SVM first (fast path)
	svm, err := m.client.GetSVM(ctx, svmName)
	if err == nil {
		klog.V(4).Infof("SVM %s already exists (VIP: %s, state: %s)", svmName, svm.VIP, svm.State)
		return m.waitForReadySVM(ctx, svmName, svm)
	}

	if err != nil && !errors.Is(err, ErrSVMNotFound) {
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
		klog.V(4).Infof("SVM %s was created by another controller (state: %s)", svmName, svm.State)
		return m.waitForReadySVM(ctx, svmName, svm)
	}

	if err != nil && !errors.Is(err, ErrSVMNotFound) {
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
			return m.waitForReadySVM(ctx, svmName, svm)
		}

		// Check error type
		if errors.Is(err, ErrSVMAlreadyExists) {
			// Another controller created it concurrently
			svm, getErr := m.client.GetSVM(ctx, svmName)
			if getErr == nil {
				return m.waitForReadySVM(ctx, svmName, svm)
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

func (m *SVMManager) waitForReadySVM(ctx context.Context, svmName string, svm *SVM) (*SVM, error) {
	for {
		if isSVMReady(svm) {
			return svm, nil
		}
		if isSVMTerminalNotReady(svm) {
			return nil, fmt.Errorf("SVM %s is not ready (state: %s)", svmName, svm.State)
		}

		state := "unknown"
		if svm != nil {
			state = svm.State
		}
		klog.V(4).Infof("Waiting for SVM %s to become ready (state: %s)", svmName, state)

		timer := time.NewTimer(svmReadyPollInterval)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil, fmt.Errorf("waiting for SVM %s to become ready: %w", svmName, ctx.Err())
		case <-timer.C:
		}

		var err error
		svm, err = m.client.GetSVM(ctx, svmName)
		if err != nil {
			return nil, fmt.Errorf("failed to poll SVM readiness for %s: %w", svmName, err)
		}
	}
}

func isSVMReady(svm *SVM) bool {
	if svm == nil {
		return false
	}
	return strings.EqualFold(svm.State, "ready") || strings.EqualFold(svm.State, "available")
}

func isSVMTerminalNotReady(svm *SVM) bool {
	if svm == nil {
		return false
	}
	return strings.EqualFold(svm.State, "failed") || strings.EqualFold(svm.State, "error") || strings.EqualFold(svm.State, "deleting")
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
	svmName := svmNameForNamespace(namespace)
	return m.client.GetSVM(ctx, svmName)
}

func svmNameForNamespace(namespace string) string {
	name := svmNamePrefix + namespace
	if len(name) <= maxArcaSVMNameBytes {
		return name
	}

	sum := sha256.Sum256([]byte(namespace))
	suffix := "-" + hex.EncodeToString(sum[:svmNameHashBytes])
	headLen := maxArcaSVMNameBytes - len(suffix)
	return name[:headLen] + suffix
}
