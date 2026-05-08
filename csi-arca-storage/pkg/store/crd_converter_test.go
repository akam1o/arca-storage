package store

import (
	"testing"
	"time"

	"github.com/akam1o/csi-arca-storage/pkg/apis/storage/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestArcaVolumeReadinessAnnotationRoundTrip(t *testing.T) {
	volume := volumeInfoToArcaVolume(&VolumeInfo{
		VolumeID:      "pvc-0123456789abcdef",
		Name:          "pvc-a",
		SVMName:       "svm-a",
		VIP:           "10.0.0.10",
		Path:          "pvc-0123456789abcdef",
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
		VolumeID:                       "pvc-0123456789abcdef",
		Name:                           "pvc-a",
		SVMName:                        "svm-a",
		VIP:                            "10.0.0.10",
		Path:                           "pvc-0123456789abcdef",
		CapacityBytes:                  1 << 30,
		CreatedAt:                      time.Now(),
		TemporaryCloneSnapshot:         "clone-pvc-0123456789abcdef-0123456789abcdef",
		TemporaryCloneSourceVolumePath: "source-path",
	})

	if got := volume.Annotations[temporaryCloneSnapshotAnnotation]; got != "clone-pvc-0123456789abcdef-0123456789abcdef" {
		t.Fatalf("temporary clone snapshot annotation = %q", got)
	}
	if got := volume.Annotations[temporaryCloneSourceVolumePathAnnotation]; got != "source-path" {
		t.Fatalf("temporary clone source path annotation = %q", got)
	}
	info := arcaVolumeToVolumeInfo(volume)
	if info.TemporaryCloneSnapshot != "clone-pvc-0123456789abcdef-0123456789abcdef" || info.TemporaryCloneSourceVolumePath != "source-path" {
		t.Fatalf("temporary clone metadata = (%q, %q)", info.TemporaryCloneSnapshot, info.TemporaryCloneSourceVolumePath)
	}
}

func TestArcaVolumeMissingReadinessAnnotationIsReady(t *testing.T) {
	info := arcaVolumeToVolumeInfo(&v1alpha1.ArcaVolume{
		Spec: v1alpha1.ArcaVolumeSpec{
			VolumeID:      "pvc-0123456789abcdef",
			Name:          "pvc-a",
			SVMName:       "svm-a",
			VIP:           "10.0.0.10",
			Path:          "pvc-0123456789abcdef",
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
		SnapshotID:       "0123456789abcdef",
		Name:             "snap-a",
		SourceVolumeID:   "pvc-0123456789abcdef",
		SourceVolumePath: "pvc-0123456789abcdef",
		SVMName:          "svm-a",
		Path:             "0123456789abcdef",
		SizeBytes:        1 << 30,
		CreatedAt:        time.Now(),
		ReadyToUse:       true,
	})

	if got := snapshot.Spec.SourceVolumePath; got != "pvc-0123456789abcdef" {
		t.Fatalf("source volume path = %q", got)
	}
	info := arcaSnapshotToSnapshotInfo(snapshot)
	if info.SourceVolumePath != "pvc-0123456789abcdef" {
		t.Fatalf("round-tripped source volume path = %q", info.SourceVolumePath)
	}
}
