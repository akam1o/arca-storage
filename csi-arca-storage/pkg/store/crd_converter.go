// SPDX-License-Identifier: Apache-2.0

package store

import (
	"github.com/akam1o/csi-arca-storage/pkg/apis/storage/v1alpha1"
	"github.com/container-storage-interface/spec/lib/go/csi"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// convertContentSourceToCRD converts CSI VolumeContentSource to CRD ArcaContentSource
func convertContentSourceToCRD(source *csi.VolumeContentSource) *v1alpha1.ArcaContentSource {
	if source == nil {
		return nil
	}

	result := &v1alpha1.ArcaContentSource{}

	if vol := source.GetVolume(); vol != nil {
		result.Type = v1alpha1.ArcaContentSourceTypeVolume
		result.SourceVolumeID = &vol.VolumeId
	} else if snap := source.GetSnapshot(); snap != nil {
		result.Type = v1alpha1.ArcaContentSourceTypeSnapshot
		result.SourceSnapshotID = &snap.SnapshotId
	}

	return result
}

// convertContentSourceFromCRD converts CRD ArcaContentSource to CSI VolumeContentSource
func convertContentSourceFromCRD(source *v1alpha1.ArcaContentSource) *csi.VolumeContentSource {
	if source == nil {
		return nil
	}

	switch source.Type {
	case v1alpha1.ArcaContentSourceTypeVolume:
		if source.SourceVolumeID != nil {
			return &csi.VolumeContentSource{
				Type: &csi.VolumeContentSource_Volume{
					Volume: &csi.VolumeContentSource_VolumeSource{
						VolumeId: *source.SourceVolumeID,
					},
				},
			}
		}
	case v1alpha1.ArcaContentSourceTypeSnapshot:
		if source.SourceSnapshotID != nil {
			return &csi.VolumeContentSource{
				Type: &csi.VolumeContentSource_Snapshot{
					Snapshot: &csi.VolumeContentSource_SnapshotSource{
						SnapshotId: *source.SourceSnapshotID,
					},
				},
			}
		}
	}

	return nil
}

// volumeInfoToArcaVolume converts VolumeInfo to ArcaVolume CRD
func volumeInfoToArcaVolume(info *VolumeInfo) *v1alpha1.ArcaVolume {
	return &v1alpha1.ArcaVolume{
		ObjectMeta: metav1.ObjectMeta{
			Name: info.VolumeID,
			Labels: map[string]string{
				"storage.arca.io/volume-id": info.VolumeID,
			},
		},
		Spec: v1alpha1.ArcaVolumeSpec{
			VolumeID:      info.VolumeID,
			Name:          info.Name,
			SVMName:       info.SVMName,
			VIP:           info.VIP,
			Path:          info.Path,
			CapacityBytes: info.CapacityBytes,
			CreatedAt:     metav1.NewTime(info.CreatedAt),
			ContentSource: convertContentSourceToCRD(info.ContentSource),
		},
		Status: v1alpha1.ArcaVolumeStatus{},
	}
}

// arcaVolumeToVolumeInfo converts ArcaVolume CRD to VolumeInfo
func arcaVolumeToVolumeInfo(av *v1alpha1.ArcaVolume) *VolumeInfo {
	return &VolumeInfo{
		VolumeID:      av.Spec.VolumeID,
		Name:          av.Spec.Name,
		SVMName:       av.Spec.SVMName,
		VIP:           av.Spec.VIP,
		Path:          av.Spec.Path,
		CapacityBytes: av.Spec.CapacityBytes,
		CreatedAt:     av.Spec.CreatedAt.Time,
		ContentSource: convertContentSourceFromCRD(av.Spec.ContentSource),
	}
}

// snapshotInfoToArcaSnapshot converts SnapshotInfo to ArcaSnapshot CRD
func snapshotInfoToArcaSnapshot(info *SnapshotInfo) *v1alpha1.ArcaSnapshot {
	return &v1alpha1.ArcaSnapshot{
		ObjectMeta: metav1.ObjectMeta{
			Name: info.SnapshotID,
			Labels: map[string]string{
				"storage.arca.io/snapshot-id":      info.SnapshotID,
				"storage.arca.io/source-volume-id": info.SourceVolumeID,
			},
		},
		Spec: v1alpha1.ArcaSnapshotSpec{
			SnapshotID:     info.SnapshotID,
			Name:           info.Name,
			SourceVolumeID: info.SourceVolumeID,
			SVMName:        info.SVMName,
			Path:           info.Path,
			SizeBytes:      info.SizeBytes,
			CreatedAt:      metav1.NewTime(info.CreatedAt),
		},
		Status: v1alpha1.ArcaSnapshotStatus{
			ReadyToUse: info.ReadyToUse,
		},
	}
}

// arcaSnapshotToSnapshotInfo converts ArcaSnapshot CRD to SnapshotInfo
func arcaSnapshotToSnapshotInfo(as *v1alpha1.ArcaSnapshot) *SnapshotInfo {
	return &SnapshotInfo{
		SnapshotID:     as.Spec.SnapshotID,
		Name:           as.Spec.Name,
		SourceVolumeID: as.Spec.SourceVolumeID,
		SVMName:        as.Spec.SVMName,
		Path:           as.Spec.Path,
		SizeBytes:      as.Spec.SizeBytes,
		CreatedAt:      as.Spec.CreatedAt.Time,
		ReadyToUse:     as.Status.ReadyToUse,
	}
}
