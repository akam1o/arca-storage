package store

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/akam1o/csi-arca-storage/pkg/apis/storage/v1alpha1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrlfake "sigs.k8s.io/controller-runtime/pkg/client/fake"
)

func TestCRDStoreCreateVolumeRejectsUnsafePath(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := v1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("AddToScheme() error = %v", err)
	}
	st := &CRDStore{
		client: ctrlfake.NewClientBuilder().WithScheme(scheme).Build(),
	}

	err := st.CreateVolume(context.Background(), &VolumeInfo{
		VolumeID:      testVolumeID,
		Name:          "pvc-a",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		Path:          "/path-a",
		CapacityBytes: 1 << 30,
		CreatedAt:     time.Now(),
	})
	if err == nil {
		t.Fatal("CreateVolume() error = nil, want unsafe path error")
	}
	if !strings.Contains(err.Error(), "volume path must be a relative path") {
		t.Fatalf("CreateVolume() error = %v, want relative path validation", err)
	}
}

func TestCRDStoreHonorsCanceledContextBeforeKubernetesCall(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := v1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("AddToScheme() error = %v", err)
	}
	st := &CRDStore{
		client: ctrlfake.NewClientBuilder().WithScheme(scheme).Build(),
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	err := st.CreateVolume(ctx, &VolumeInfo{
		VolumeID:      testVolumeID,
		Name:          "pvc-a",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		Path:          "path-a",
		CapacityBytes: 1 << 30,
		CreatedAt:     time.Now(),
	})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("CreateVolume() error = %v, want context.Canceled", err)
	}

	_, err = st.GetVolume(ctx, testVolumeID)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("GetVolume() error = %v, want context.Canceled", err)
	}
}

func TestCRDStoreGetVolumeRejectsUnsafeStoredPath(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := v1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("AddToScheme() error = %v", err)
	}

	existing := volumeInfoToArcaVolume(&VolumeInfo{
		VolumeID:      testVolumeID,
		Name:          "pvc-a",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		Path:          "path-a",
		CapacityBytes: 1 << 30,
		CreatedAt:     time.Now(),
	})
	existing.Spec.Path = "volumes/../path-a"
	st := &CRDStore{
		client: ctrlfake.NewClientBuilder().WithScheme(scheme).WithObjects(existing).Build(),
	}

	_, err := st.GetVolume(context.Background(), testVolumeID)
	if err == nil {
		t.Fatal("GetVolume() error = nil, want unsafe stored path error")
	}
	if !strings.Contains(err.Error(), "invalid stored ArcaVolume") || !strings.Contains(err.Error(), "volume path must be canonical") {
		t.Fatalf("GetVolume() error = %v, want invalid stored ArcaVolume canonical path error", err)
	}
}

func TestCRDStoreListVolumesRejectsUnsafeStoredPath(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := v1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("AddToScheme() error = %v", err)
	}

	existing := volumeInfoToArcaVolume(&VolumeInfo{
		VolumeID:      testVolumeID,
		Name:          "pvc-a",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		Path:          "path-a",
		CapacityBytes: 1 << 30,
		CreatedAt:     time.Now(),
	})
	existing.Spec.Path = "/path-a"
	st := &CRDStore{
		client: ctrlfake.NewClientBuilder().WithScheme(scheme).WithObjects(existing).Build(),
	}

	_, _, err := st.ListVolumes(context.Background(), "", 0)
	if err == nil {
		t.Fatal("ListVolumes() error = nil, want unsafe stored path error")
	}
	if !strings.Contains(err.Error(), "invalid stored ArcaVolume") || !strings.Contains(err.Error(), "volume path must be a relative path") {
		t.Fatalf("ListVolumes() error = %v, want invalid stored ArcaVolume relative path error", err)
	}
}

func TestCRDStoreGetSnapshotRejectsUnsafeStoredSourceVolumePath(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := v1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("AddToScheme() error = %v", err)
	}

	existing := snapshotInfoToArcaSnapshot(&SnapshotInfo{
		SnapshotID:       testSnapshotID,
		Name:             "snap-a",
		SourceVolumeID:   testVolumeID,
		SourceVolumePath: testVolumeID,
		SVMName:          "svm-a",
		Path:             testSnapshotID,
		SizeBytes:        1 << 30,
		CreatedAt:        time.Now(),
		ReadyToUse:       true,
	})
	existing.Spec.SourceVolumePath = "volumes/../" + testVolumeID
	st := &CRDStore{
		client: ctrlfake.NewClientBuilder().WithScheme(scheme).WithObjects(existing).Build(),
	}

	_, err := st.GetSnapshot(context.Background(), testSnapshotID)
	if err == nil {
		t.Fatal("GetSnapshot() error = nil, want unsafe stored source path error")
	}
	if !strings.Contains(err.Error(), "invalid stored ArcaSnapshot") || !strings.Contains(err.Error(), "snapshot source volume path must be canonical") {
		t.Fatalf("GetSnapshot() error = %v, want invalid stored ArcaSnapshot canonical source path error", err)
	}
}

func TestCRDStoreUpdateVolumePreservesLargerCapacity(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := v1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("AddToScheme() error = %v", err)
	}

	existing := volumeInfoToArcaVolume(&VolumeInfo{
		VolumeID:      "vol-a",
		Name:          "pvc-a",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		Path:          "path-a",
		CapacityBytes: 20 << 30,
		CreatedAt:     time.Now(),
	})
	st := &CRDStore{
		client: ctrlfake.NewClientBuilder().WithScheme(scheme).WithObjects(existing).Build(),
	}

	err := st.UpdateVolume(context.Background(), &VolumeInfo{
		VolumeID:      "vol-a",
		Name:          "pvc-a",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		Path:          "path-a",
		CapacityBytes: 10 << 30,
		CreatedAt:     existing.Spec.CreatedAt.Time,
	})
	if err != nil {
		t.Fatalf("UpdateVolume() error = %v", err)
	}

	stored, err := st.GetVolume(context.Background(), "vol-a")
	if err != nil {
		t.Fatalf("GetVolume() error = %v", err)
	}
	if stored.CapacityBytes != 20<<30 {
		t.Fatalf("capacity = %d, want %d", stored.CapacityBytes, int64(20<<30))
	}
}

func TestCRDStoreUpdateVolumeClearsTemporaryCloneAnnotations(t *testing.T) {
	scheme := runtime.NewScheme()
	if err := v1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("AddToScheme() error = %v", err)
	}

	existing := volumeInfoToArcaVolume(&VolumeInfo{
		VolumeID:                       "vol-a",
		Name:                           "pvc-a",
		SVMName:                        "svm-a",
		VIP:                            "10.0.0.10",
		Path:                           "path-a",
		CapacityBytes:                  20 << 30,
		CreatedAt:                      time.Now(),
		TemporaryCloneSnapshot:         "clone-vol-a-0123456789abcdef",
		TemporaryCloneSourceVolumePath: "source-path",
	})
	st := &CRDStore{
		client: ctrlfake.NewClientBuilder().WithScheme(scheme).WithObjects(existing).Build(),
	}

	err := st.UpdateVolume(context.Background(), &VolumeInfo{
		VolumeID:      "vol-a",
		Name:          "pvc-a",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		Path:          "path-a",
		CapacityBytes: 20 << 30,
		CreatedAt:     existing.Spec.CreatedAt.Time,
	})
	if err != nil {
		t.Fatalf("UpdateVolume() error = %v", err)
	}

	stored, err := st.GetVolume(context.Background(), "vol-a")
	if err != nil {
		t.Fatalf("GetVolume() error = %v", err)
	}
	if stored.TemporaryCloneSnapshot != "" || stored.TemporaryCloneSourceVolumePath != "" {
		t.Fatalf("temporary clone metadata = (%q, %q), want empty", stored.TemporaryCloneSnapshot, stored.TemporaryCloneSourceVolumePath)
	}
}
