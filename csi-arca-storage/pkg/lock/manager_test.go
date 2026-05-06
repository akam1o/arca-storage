package lock

import (
	"context"
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
