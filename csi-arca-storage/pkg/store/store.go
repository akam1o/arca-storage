// SPDX-License-Identifier: Apache-2.0

package store

import "context"

// Store defines the interface for volume/snapshot metadata storage.
// Implementations include MemoryStore (in-memory) and CRDStore (persistent via Kubernetes CRDs).
type Store interface {
	// Volume operations
	CreateVolume(ctx context.Context, info *VolumeInfo) error
	UpdateVolume(ctx context.Context, info *VolumeInfo) error
	GetVolume(ctx context.Context, volumeID string) (*VolumeInfo, error)
	DeleteVolume(ctx context.Context, volumeID string) error
	ListVolumes(ctx context.Context, startingToken string, maxEntries int) ([]*VolumeInfo, string, error)

	// Snapshot operations
	CreateSnapshot(ctx context.Context, info *SnapshotInfo) error
	UpdateSnapshotStatus(ctx context.Context, snapshotID string, readyToUse bool) error
	GetSnapshot(ctx context.Context, snapshotID string) (*SnapshotInfo, error)
	DeleteSnapshot(ctx context.Context, snapshotID string) error
	ListSnapshots(ctx context.Context, sourceVolumeID, startingToken string, maxEntries int) ([]*SnapshotInfo, string, error)
}
