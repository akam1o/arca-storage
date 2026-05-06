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
