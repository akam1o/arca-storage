package lock

import (
	"context"
	"strings"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
)

func TestReleaseSkipsLeaseHeldByAnotherHolder(t *testing.T) {
	ctx := context.Background()
	clientset := fake.NewClientset()
	manager := NewManager(clientset, "kube-system", "controller-a")

	lock, err := manager.AcquireLock(ctx, "tenant-a", 10*time.Second)
	if err != nil {
		t.Fatalf("AcquireLock() error = %v", err)
	}

	leaseClient := clientset.CoordinationV1().Leases("kube-system")
	lease, err := leaseClient.Get(ctx, lock.leaseName, metav1.GetOptions{})
	if err != nil {
		t.Fatalf("Get() error = %v", err)
	}
	otherHolder := "controller-b"
	lease.Spec.HolderIdentity = &otherHolder
	if _, err := leaseClient.Update(ctx, lease, metav1.UpdateOptions{}); err != nil {
		t.Fatalf("Update() error = %v", err)
	}

	if err := lock.Release(ctx); err != nil {
		t.Fatalf("Release() error = %v", err)
	}

	lease, err = leaseClient.Get(ctx, lock.leaseName, metav1.GetOptions{})
	if err != nil {
		t.Fatalf("lease was deleted: %v", err)
	}
	if lease.Spec.HolderIdentity == nil || *lease.Spec.HolderIdentity != otherHolder {
		t.Fatalf("holder = %v, want %q", lease.Spec.HolderIdentity, otherHolder)
	}
}

func TestAcquireLockRenewalSurvivesAcquireContextCancellation(t *testing.T) {
	acquireCtx, cancelAcquire := context.WithCancel(context.Background())
	clientset := fake.NewClientset()
	manager := NewManager(clientset, "kube-system", "controller-a")

	lock, err := manager.AcquireLock(acquireCtx, "tenant-a", 3*time.Second)
	if err != nil {
		t.Fatalf("AcquireLock() error = %v", err)
	}
	defer func() {
		releaseCtx, cancelRelease := context.WithTimeout(context.Background(), time.Second)
		defer cancelRelease()
		if err := lock.Release(releaseCtx); err != nil {
			t.Fatalf("Release() error = %v", err)
		}
	}()

	leaseClient := clientset.CoordinationV1().Leases("kube-system")
	before, err := leaseClient.Get(context.Background(), lock.leaseName, metav1.GetOptions{})
	if err != nil {
		t.Fatalf("Get() error = %v", err)
	}
	if before.Spec.RenewTime == nil {
		t.Fatalf("lease has no initial renew time")
	}

	cancelAcquire()

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		after, err := leaseClient.Get(context.Background(), lock.leaseName, metav1.GetOptions{})
		if err != nil {
			t.Fatalf("Get() after cancel error = %v", err)
		}
		if after.Spec.RenewTime != nil && after.Spec.RenewTime.After(before.Spec.RenewTime.Time) {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}

	t.Fatalf("lease was not renewed after acquisition context cancellation")
}

func TestAcquireLockBoundsLongLeaseNames(t *testing.T) {
	ctx := context.Background()
	clientset := fake.NewClientset()
	manager := NewManager(clientset, "kube-system", "controller-a")
	resourceName := strings.Repeat("a", 63)

	lock, err := manager.AcquireLock(ctx, resourceName, 10*time.Second)
	if err != nil {
		t.Fatalf("AcquireLock() error = %v", err)
	}
	defer func() {
		if err := lock.Release(ctx); err != nil {
			t.Fatalf("Release() error = %v", err)
		}
	}()

	if len(lock.leaseName) > maxLeaseNameLength {
		t.Fatalf("lease name length = %d, want <= %d: %q", len(lock.leaseName), maxLeaseNameLength, lock.leaseName)
	}
	if !strings.HasPrefix(lock.leaseName, leaseNamePrefix) {
		t.Fatalf("lease name %q does not start with %q", lock.leaseName, leaseNamePrefix)
	}
	if lock.leaseName != leaseNameForResource(resourceName) {
		t.Fatalf("lease name generation is not stable")
	}
}
