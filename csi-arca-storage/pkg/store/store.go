// SPDX-License-Identifier: Apache-2.0

package store

// Store defines the interface for volume/snapshot metadata storage.
// Implementations include MemoryStore (in-memory) and CRDStore (persistent via Kubernetes CRDs).
type Store interface {
	// Volume operations
	CreateVolume(info *VolumeInfo) error
	UpdateVolume(info *VolumeInfo) error
	GetVolume(volumeID string) (*VolumeInfo, error)
	DeleteVolume(volumeID string) error
	ListVolumes(startingToken string, maxEntries int) ([]*VolumeInfo, string, error)

	// Snapshot operations
	CreateSnapshot(info *SnapshotInfo) error
	UpdateSnapshotStatus(snapshotID string, readyToUse bool) error
	GetSnapshot(snapshotID string) (*SnapshotInfo, error)
	DeleteSnapshot(snapshotID string) error
	ListSnapshots(sourceVolumeID, startingToken string, maxEntries int) ([]*SnapshotInfo, string, error)
}
