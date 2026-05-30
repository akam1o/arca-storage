package store

import (
	"testing"
	"time"

	"github.com/akam1o/csi-arca-storage/pkg/apis/storage/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	testVolumeID               = "pvc-0123456789abcdef0123456789abcdef"
	testSnapshotID             = "0123456789abcdef0123456789abcdef"
	testTemporaryCloneSnapshot = "clone-" + testVolumeID + "-0123456789abcdef"
)

func TestArcaVolumeReadinessAnnotationRoundTrip(t *testing.T) {
	volume := volumeInfoToArcaVolume(&VolumeInfo{
		VolumeID:      testVolumeID,
		Name:          "pvc-a",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		Path:          testVolumeID,
		CapacityBytes: 1 << 30,
		CreatedAt:     time.Now(),
		ReadyToUse:    VolumeReadyState(false),
	})

	if got := volume.Annotations[volumeReadyToUseAnnotation]; got != "false" {
		t.Fatalf("ready annotation = %q, want false", got)
	}
	info := arcaVolumeToVolumeInfo(volume)
	if info.ReadyToUse == nil || *info.ReadyToUse {
		t.Fatalf("ready state = %#v, want false pointer", info.ReadyToUse)
	}
}

func TestArcaVolumeTemporaryCloneAnnotationRoundTrip(t *testing.T) {
	volume := volumeInfoToArcaVolume(&VolumeInfo{
		VolumeID:                       testVolumeID,
		Name:                           "pvc-a",
		SVMName:                        "svm-a",
		VIP:                            "10.0.0.10",
		Path:                           testVolumeID,
		CapacityBytes:                  1 << 30,
		CreatedAt:                      time.Now(),
		TemporaryCloneSnapshot:         testTemporaryCloneSnapshot,
		TemporaryCloneSourceVolumePath: "source-path",
		TemporaryCloneCleanupOnly:      true,
	})

	if got := volume.Annotations[temporaryCloneSnapshotAnnotation]; got != testTemporaryCloneSnapshot {
		t.Fatalf("temporary clone snapshot annotation = %q", got)
	}
	if got := volume.Annotations[temporaryCloneSourceVolumePathAnnotation]; got != "source-path" {
		t.Fatalf("temporary clone source path annotation = %q", got)
	}
	if got := volume.Annotations[temporaryCloneCleanupOnlyAnnotation]; got != "true" {
		t.Fatalf("temporary clone cleanup-only annotation = %q", got)
	}
	info := arcaVolumeToVolumeInfo(volume)
	if info.TemporaryCloneSnapshot != testTemporaryCloneSnapshot || info.TemporaryCloneSourceVolumePath != "source-path" || !info.TemporaryCloneCleanupOnly {
		t.Fatalf("temporary clone metadata = (%q, %q, %t)", info.TemporaryCloneSnapshot, info.TemporaryCloneSourceVolumePath, info.TemporaryCloneCleanupOnly)
	}
}

func TestArcaVolumeMissingReadinessAnnotationIsReady(t *testing.T) {
	info := arcaVolumeToVolumeInfo(&v1alpha1.ArcaVolume{
		Spec: v1alpha1.ArcaVolumeSpec{
			VolumeID:      testVolumeID,
			Name:          "pvc-a",
			SVMName:       "svm-a",
			VIP:           "10.0.0.10",
			Path:          testVolumeID,
			CapacityBytes: 1 << 30,
			CreatedAt:     metav1.NewTime(time.Now()),
		},
	})

	if !IsVolumeReady(info) {
		t.Fatalf("legacy volume without readiness annotation should be ready")
	}
}

func TestArcaSnapshotSourceVolumePathRoundTrip(t *testing.T) {
	snapshot := snapshotInfoToArcaSnapshot(&SnapshotInfo{
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

	if got := snapshot.Spec.SourceVolumePath; got != testVolumeID {
		t.Fatalf("source volume path = %q", got)
	}
	info := arcaSnapshotToSnapshotInfo(snapshot)
	if info.SourceVolumePath != testVolumeID {
		t.Fatalf("round-tripped source volume path = %q", info.SourceVolumePath)
	}
}
