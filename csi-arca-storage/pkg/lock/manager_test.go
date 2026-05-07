package lock

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
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

func TestAcquireLockRetriesExpiredLeaseUpdateConflict(t *testing.T) {
	ctx := context.Background()
	clientset := fake.NewClientset()
	manager := NewManager(clientset, "kube-system", "controller-a")
	leaseName := leaseNameForResource("tenant-a")
	oldHolder := "controller-b"
	leaseDuration := int32(1)
	renewTime := metav1.NewMicroTime(time.Now().Add(-10 * time.Second))

	leaseClient := clientset.CoordinationV1().Leases("kube-system")
	if _, err := leaseClient.Create(ctx, &coordinationv1.Lease{
		ObjectMeta: metav1.ObjectMeta{
			Name:      leaseName,
			Namespace: "kube-system",
		},
		Spec: coordinationv1.LeaseSpec{
			HolderIdentity:       &oldHolder,
			LeaseDurationSeconds: &leaseDuration,
			RenewTime:            &renewTime,
		},
	}, metav1.CreateOptions{}); err != nil {
		t.Fatalf("Create() error = %v", err)
	}

	updateCount := 0
	clientset.PrependReactor("update", "leases", func(action k8stesting.Action) (bool, runtime.Object, error) {
		updateCount++
		if updateCount == 1 {
			return true, nil, apierrors.NewConflict(
				schema.GroupResource{Group: "coordination.k8s.io", Resource: "leases"},
				leaseName,
				fmt.Errorf("stale resource version"),
			)
		}
		return false, nil, nil
	})

	lock, err := manager.AcquireLock(ctx, "tenant-a", 3*time.Second)
	if err != nil {
		t.Fatalf("AcquireLock() error = %v", err)
	}
	defer func() {
		if err := lock.Release(ctx); err != nil {
			t.Fatalf("Release() error = %v", err)
		}
	}()

	if updateCount < 2 {
		t.Fatalf("update count = %d, want retry after conflict", updateCount)
	}
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
