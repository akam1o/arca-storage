package store

import (
	"testing"
	"time"

	"github.com/akam1o/csi-arca-storage/pkg/apis/storage/v1alpha1"
	"k8s.io/apimachinery/pkg/runtime"
	ctrlfake "sigs.k8s.io/controller-runtime/pkg/client/fake"
)

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

	err := st.UpdateVolume(&VolumeInfo{
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

	stored, err := st.GetVolume("vol-a")
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

	err := st.UpdateVolume(&VolumeInfo{
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

	stored, err := st.GetVolume("vol-a")
	if err != nil {
		t.Fatalf("GetVolume() error = %v", err)
	}
	if stored.TemporaryCloneSnapshot != "" || stored.TemporaryCloneSourceVolumePath != "" {
		t.Fatalf("temporary clone metadata = (%q, %q), want empty", stored.TemporaryCloneSnapshot, stored.TemporaryCloneSourceVolumePath)
	}
}
