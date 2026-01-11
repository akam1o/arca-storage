// SPDX-License-Identifier: Apache-2.0

package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

type ArcaContentSourceType string

const (
	ArcaContentSourceTypeVolume   ArcaContentSourceType = "Volume"
	ArcaContentSourceTypeSnapshot ArcaContentSourceType = "Snapshot"
)

// ArcaContentSource is a flattened content source reference for cloning/restoring.
//
// +kubebuilder:validation:XValidation:rule="(self.type == 'Volume' && has(self.sourceVolumeID) && !has(self.sourceSnapshotID)) || (self.type == 'Snapshot' && has(self.sourceSnapshotID) && !has(self.sourceVolumeID))",message="contentSource must set exactly one of sourceVolumeID/sourceSnapshotID matching type"
type ArcaContentSource struct {
	// Type specifies the source kind.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Enum=Volume;Snapshot
	Type ArcaContentSourceType `json:"type"`

	// SourceVolumeID is required when type=Volume.
	// +kubebuilder:validation:Optional
	// +kubebuilder:validation:Pattern=`^pvc-[a-f0-9]{16}$`
	// +kubebuilder:validation:MinLength=20
	// +kubebuilder:validation:MaxLength=20
	SourceVolumeID *string `json:"sourceVolumeID,omitempty"`

	// SourceSnapshotID is required when type=Snapshot.
	// +kubebuilder:validation:Optional
	// +kubebuilder:validation:Pattern=`^[a-f0-9]{16}$`
	// +kubebuilder:validation:MinLength=16
	// +kubebuilder:validation:MaxLength=16
	SourceSnapshotID *string `json:"sourceSnapshotID,omitempty"`
}

type ArcaVolumeSpec struct {
	// VolumeID is the ARCA backend identifier for this volume.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Pattern=`^pvc-[a-f0-9]{16}$`
	// +kubebuilder:validation:MinLength=20
	// +kubebuilder:validation:MaxLength=20
	VolumeID string `json:"volumeID"`

	// Name is a human-friendly name for the volume (distinct from metadata.name).
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=253
	// +kubebuilder:validation:Pattern=`^[A-Za-z0-9]([A-Za-z0-9_.-]{0,251}[A-Za-z0-9])?$`
	Name string `json:"name"`

	// SVMName is the storage virtual machine name.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=63
	// +kubebuilder:validation:Pattern=`^[A-Za-z0-9]([A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?$`
	SVMName string `json:"svmName"`

	// VIP is the virtual IP address used to access the storage endpoint.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Format=ip
	// +kubebuilder:validation:MaxLength=45
	VIP string `json:"vip"`

	// Path is the backend path/location of the volume (relative path, no leading slash).
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=4096
	Path string `json:"path"`

	// CapacityBytes is the provisioned capacity in bytes.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Minimum=1
	CapacityBytes int64 `json:"capacityBytes"`

	// CreatedAt is the backend creation timestamp.
	// +kubebuilder:validation:Required
	CreatedAt metav1.Time `json:"createdAt"`

	// ContentSource describes the source used to create this volume (clone/restore).
	// +kubebuilder:validation:Optional
	ContentSource *ArcaContentSource `json:"contentSource,omitempty"`
}

type ArcaVolumeStatus struct {
	// ObservedGeneration is the most recent generation observed for this resource.
	// +kubebuilder:validation:Optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// Conditions represent the latest available observations of this resource's state.
	// +kubebuilder:validation:Optional
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// ArcaVolume is a cluster-scoped persistent record of an ARCA volume.
//
// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Cluster,path=arcavolumes,singular=arcavolume,shortName=av,categories=storage;arca
// +kubebuilder:subresource:status
// +kubebuilder:storageversion
// +kubebuilder:printcolumn:name="VolumeID",type="string",JSONPath=".spec.volumeID",description="Backend volume identifier"
// +kubebuilder:printcolumn:name="SVM",type="string",JSONPath=".spec.svmName",description="Storage virtual machine"
// +kubebuilder:printcolumn:name="VIP",type="string",JSONPath=".spec.vip",description="Storage endpoint VIP"
// +kubebuilder:printcolumn:name="Path",type="string",JSONPath=".spec.path",description="Backend path"
// +kubebuilder:printcolumn:name="CapacityBytes",type="integer",JSONPath=".spec.capacityBytes",description="Provisioned capacity (bytes)"
// +kubebuilder:printcolumn:name="Age",type="date",JSONPath=".metadata.creationTimestamp"
type ArcaVolume struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   ArcaVolumeSpec   `json:"spec"`
	Status ArcaVolumeStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type ArcaVolumeList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ArcaVolume `json:"items"`
}

type ArcaSnapshotSpec struct {
	// SnapshotID is the ARCA backend identifier for this snapshot.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Pattern=`^[a-f0-9]{16}$`
	// +kubebuilder:validation:MinLength=16
	// +kubebuilder:validation:MaxLength=16
	SnapshotID string `json:"snapshotID"`

	// Name is a human-friendly name for the snapshot (distinct from metadata.name).
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=253
	// +kubebuilder:validation:Pattern=`^[A-Za-z0-9]([A-Za-z0-9_.-]{0,251}[A-Za-z0-9])?$`
	Name string `json:"name"`

	// SourceVolumeID is the backend identifier of the volume this snapshot was taken from.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Pattern=`^pvc-[a-f0-9]{16}$`
	// +kubebuilder:validation:MinLength=20
	// +kubebuilder:validation:MaxLength=20
	SourceVolumeID string `json:"sourceVolumeID"`

	// SVMName is the storage virtual machine name.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=63
	// +kubebuilder:validation:Pattern=`^[A-Za-z0-9]([A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?$`
	SVMName string `json:"svmName"`

	// Path is the backend path/location of the snapshot (relative path, no leading slash).
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=4096
	Path string `json:"path"`

	// SizeBytes is the logical size of the snapshot in bytes.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Minimum=1
	SizeBytes int64 `json:"sizeBytes"`

	// CreatedAt is the backend creation timestamp.
	// +kubebuilder:validation:Required
	CreatedAt metav1.Time `json:"createdAt"`
}

type ArcaSnapshotStatus struct {
	// ObservedGeneration is the most recent generation observed for this resource.
	// +kubebuilder:validation:Optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// ReadyToUse indicates the snapshot is ready for use.
	// +kubebuilder:validation:Optional
	ReadyToUse bool `json:"readyToUse,omitempty"`

	// Conditions represent the latest available observations of this resource's state.
	// +kubebuilder:validation:Optional
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// ArcaSnapshot is a cluster-scoped persistent record of an ARCA snapshot.
//
// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Cluster,path=arcasnapshots,singular=arcasnapshot,shortName=asnap,categories=storage;arca
// +kubebuilder:subresource:status
// +kubebuilder:storageversion
// +kubebuilder:printcolumn:name="SnapshotID",type="string",JSONPath=".spec.snapshotID",description="Backend snapshot identifier"
// +kubebuilder:printcolumn:name="SourceVolumeID",type="string",JSONPath=".spec.sourceVolumeID",description="Source backend volume identifier"
// +kubebuilder:printcolumn:name="SVM",type="string",JSONPath=".spec.svmName",description="Storage virtual machine"
// +kubebuilder:printcolumn:name="Path",type="string",JSONPath=".spec.path",description="Backend path"
// +kubebuilder:printcolumn:name="SizeBytes",type="integer",JSONPath=".spec.sizeBytes",description="Snapshot size (bytes)"
// +kubebuilder:printcolumn:name="Ready",type="boolean",JSONPath=".status.readyToUse",description="Ready to use"
// +kubebuilder:printcolumn:name="Age",type="date",JSONPath=".metadata.creationTimestamp"
type ArcaSnapshot struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   ArcaSnapshotSpec   `json:"spec"`
	Status ArcaSnapshotStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type ArcaSnapshotList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ArcaSnapshot `json:"items"`
}
