package lock

import (
	"context"
	"fmt"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/klog/v2"
)

// Manager manages distributed locks using Kubernetes Leases
type Manager struct {
	clientset *kubernetes.Clientset
	namespace string
	identity  string
}

// Lock represents an acquired lock
type Lock struct {
	manager   *Manager
	leaseName string
	ctx       context.Context
	cancel    context.CancelFunc
}

// NewManager creates a new lock manager
func NewManager(clientset *kubernetes.Clientset, namespace, identity string) *Manager {
	return &Manager{
		clientset: clientset,
		namespace: namespace,
		identity:  identity,
	}
}

// AcquireLock acquires a distributed lock for the given resource
func (m *Manager) AcquireLock(ctx context.Context, resourceName string, ttl time.Duration) (*Lock, error) {
	leaseName := fmt.Sprintf("arca-csi-svm-%s", resourceName)

	lockCtx, cancel := context.WithCancel(ctx)
	lock := &Lock{
		manager:   m,
		leaseName: leaseName,
		ctx:       lockCtx,
		cancel:    cancel,
	}

	// Try to acquire the lease
	deadline := time.Now().Add(ttl)
	for time.Now().Before(deadline) {
		acquired, err := m.tryAcquireLease(ctx, leaseName, ttl)
		if err != nil {
			cancel()
			return nil, fmt.Errorf("failed to acquire lease: %w", err)
		}

		if acquired {
			// Start renewing the lease in background
			go lock.renewLoop(ttl)
			klog.V(4).Infof("Acquired lock for resource %s (lease: %s)", resourceName, leaseName)
			return lock, nil
		}

		// Wait before retry
		select {
		case <-time.After(time.Second):
		case <-ctx.Done():
			cancel()
			return nil, ctx.Err()
		}
	}

	cancel()
	return nil, fmt.Errorf("failed to acquire lock for %s within %v", resourceName, ttl)
}

// tryAcquireLease attempts to acquire or update a lease
func (m *Manager) tryAcquireLease(ctx context.Context, leaseName string, ttl time.Duration) (bool, error) {
	leaseDuration := int32(ttl.Seconds())
	now := metav1.NewMicroTime(time.Now())

	leaseClient := m.clientset.CoordinationV1().Leases(m.namespace)

	// Try to get existing lease
	lease, err := leaseClient.Get(ctx, leaseName, metav1.GetOptions{})
	if err == nil {
		// Lease exists - check if we own it or it's expired
		if lease.Spec.HolderIdentity != nil && *lease.Spec.HolderIdentity == m.identity {
			// We own it - renew
			lease.Spec.RenewTime = &now
			_, err = leaseClient.Update(ctx, lease, metav1.UpdateOptions{})
			return err == nil, err
		}

		// Check if expired
		if lease.Spec.RenewTime != nil && lease.Spec.LeaseDurationSeconds != nil {
			renewTime := lease.Spec.RenewTime.Time
			expiryTime := renewTime.Add(time.Duration(*lease.Spec.LeaseDurationSeconds) * time.Second)
			if time.Now().After(expiryTime) {
				// Expired - take over
				lease.Spec.HolderIdentity = &m.identity
				lease.Spec.RenewTime = &now
				lease.Spec.LeaseDurationSeconds = &leaseDuration
				_, err = leaseClient.Update(ctx, lease, metav1.UpdateOptions{})
				return err == nil, err
			}
		}

		// Someone else owns it and it's not expired
		return false, nil
	}

	// Check if error is NotFound (expected) vs other errors
	if !apierrors.IsNotFound(err) {
		// Real error (RBAC, network) - don't mask it
		return false, fmt.Errorf("failed to get lease: %w", err)
	}

	// Lease doesn't exist - create it
	lease = &coordinationv1.Lease{
		ObjectMeta: metav1.ObjectMeta{
			Name:      leaseName,
			Namespace: m.namespace,
		},
		Spec: coordinationv1.LeaseSpec{
			HolderIdentity:       &m.identity,
			LeaseDurationSeconds: &leaseDuration,
			RenewTime:            &now,
		},
	}

	_, err = leaseClient.Create(ctx, lease, metav1.CreateOptions{})
	return err == nil, err
}

// renewLoop renews the lease periodically
func (l *Lock) renewLoop(ttl time.Duration) {
	ticker := time.NewTicker(ttl / 3) // Renew at 1/3 of TTL
	defer ticker.Stop()

	for {
		select {
		case <-ticker.C:
			_, err := l.manager.tryAcquireLease(l.ctx, l.leaseName, ttl)
			if err != nil {
				klog.Warningf("Failed to renew lease %s: %v", l.leaseName, err)
			}
		case <-l.ctx.Done():
			return
		}
	}
}

// Release releases the lock
func (l *Lock) Release(ctx context.Context) error {
	l.cancel() // Stop renewal

	// Delete the lease
	leaseClient := l.manager.clientset.CoordinationV1().Leases(l.manager.namespace)
	err := leaseClient.Delete(ctx, l.leaseName, metav1.DeleteOptions{})
	if err != nil {
		klog.Warningf("Failed to delete lease %s: %v", l.leaseName, err)
		return err
	}

	klog.V(4).Infof("Released lock (lease: %s)", l.leaseName)
	return nil
}
