package store

import (
	"fmt"
	"sync"
	"time"

	"github.com/container-storage-interface/spec/lib/go/csi"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// VolumeInfo represents volume metadata
type VolumeInfo struct {
	VolumeID      string
	Name          string // Original PVC name
	SVMName       string
	VIP           string
	Path          string
	CapacityBytes int64
	CreatedAt     time.Time
	ContentSource *csi.VolumeContentSource
}

// SnapshotInfo represents snapshot metadata
type SnapshotInfo struct {
	SnapshotID     string
	Name           string // Original VolumeSnapshot name
	SourceVolumeID string
	SVMName        string
	Path           string
	SizeBytes      int64
	CreatedAt      time.Time
	ReadyToUse     bool
}

// MemoryStore provides in-memory storage for volume and snapshot metadata
// NOTE: In production, this should be replaced with CRD-based persistent storage
type MemoryStore struct {
	volumes   map[string]*VolumeInfo   // volumeID -> info
	snapshots map[string]*SnapshotInfo // snapshotID -> info
	mu        sync.RWMutex
}

// NewMemoryStore creates a new memory store
func NewMemoryStore() *MemoryStore {
	return &MemoryStore{
		volumes:   make(map[string]*VolumeInfo),
		snapshots: make(map[string]*SnapshotInfo),
	}
}

// CreateVolume stores volume metadata
func (s *MemoryStore) CreateVolume(info *VolumeInfo) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	if _, exists := s.volumes[info.VolumeID]; exists {
		return fmt.Errorf("%w: volume %s", ErrAlreadyExists, info.VolumeID)
	}

	if info.CreatedAt.IsZero() {
		info.CreatedAt = time.Now()
	}
	s.volumes[info.VolumeID] = info
	return nil
}

// UpdateVolume updates existing volume metadata
func (s *MemoryStore) UpdateVolume(info *VolumeInfo) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	if _, exists := s.volumes[info.VolumeID]; !exists {
		return fmt.Errorf("%w: volume %s", ErrNotFound, info.VolumeID)
	}

	s.volumes[info.VolumeID] = info
	return nil
}

// GetVolume retrieves volume metadata
func (s *MemoryStore) GetVolume(volumeID string) (*VolumeInfo, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()

	info, exists := s.volumes[volumeID]
	if !exists {
		return nil, fmt.Errorf("%w: volume %s", ErrNotFound, volumeID)
	}

	return info, nil
}

// DeleteVolume removes volume metadata
func (s *MemoryStore) DeleteVolume(volumeID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	delete(s.volumes, volumeID)
	return nil
}

// ListVolumes returns all volumes (with optional pagination)
func (s *MemoryStore) ListVolumes(startingToken string, maxEntries int) ([]*VolumeInfo, string, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()

	var result []*VolumeInfo
	var nextToken string

	started := startingToken == ""
	count := 0

	for volumeID, info := range s.volumes {
		if !started {
			if volumeID == startingToken {
				started = true
			}
			continue
		}

		result = append(result, info)
		count++

		if maxEntries > 0 && count >= maxEntries {
			// Set next token to the next volume ID (simplified pagination)
			nextToken = volumeID
			break
		}
	}

	return result, nextToken, nil
}

// CreateSnapshot stores snapshot metadata
func (s *MemoryStore) CreateSnapshot(info *SnapshotInfo) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	if _, exists := s.snapshots[info.SnapshotID]; exists {
		return fmt.Errorf("%w: snapshot %s", ErrAlreadyExists, info.SnapshotID)
	}

	if info.CreatedAt.IsZero() {
		info.CreatedAt = time.Now()
	}
	s.snapshots[info.SnapshotID] = info
	return nil
}

// UpdateSnapshotStatus updates the ReadyToUse status of a snapshot
func (s *MemoryStore) UpdateSnapshotStatus(snapshotID string, readyToUse bool) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	snap, exists := s.snapshots[snapshotID]
	if !exists {
		return fmt.Errorf("%w: snapshot %s", ErrNotFound, snapshotID)
	}

	snap.ReadyToUse = readyToUse
	return nil
}

// GetSnapshot retrieves snapshot metadata
func (s *MemoryStore) GetSnapshot(snapshotID string) (*SnapshotInfo, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()

	info, exists := s.snapshots[snapshotID]
	if !exists {
		return nil, fmt.Errorf("%w: snapshot %s", ErrNotFound, snapshotID)
	}

	return info, nil
}

// DeleteSnapshot removes snapshot metadata
func (s *MemoryStore) DeleteSnapshot(snapshotID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	delete(s.snapshots, snapshotID)
	return nil
}

// ListSnapshots returns all snapshots (with optional filtering and pagination)
func (s *MemoryStore) ListSnapshots(sourceVolumeID, startingToken string, maxEntries int) ([]*SnapshotInfo, string, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()

	var result []*SnapshotInfo
	var nextToken string

	started := startingToken == ""
	count := 0

	for snapshotID, info := range s.snapshots {
		// Filter by source volume if specified
		if sourceVolumeID != "" && info.SourceVolumeID != sourceVolumeID {
			continue
		}

		if !started {
			if snapshotID == startingToken {
				started = true
			}
			continue
		}

		result = append(result, info)
		count++

		if maxEntries > 0 && count >= maxEntries {
			nextToken = snapshotID
			break
		}
	}

	return result, nextToken, nil
}

// ToCSIVolume converts VolumeInfo to CSI Volume
func (v *VolumeInfo) ToCSIVolume() *csi.Volume {
	return &csi.Volume{
		VolumeId:      v.VolumeID,
		CapacityBytes: v.CapacityBytes,
		VolumeContext: map[string]string{
			"svm":        v.SVMName,
			"vip":        v.VIP,
			"volumePath": v.Path,
		},
		ContentSource: v.ContentSource,
	}
}

// ToCSISnapshot converts SnapshotInfo to CSI Snapshot
func (s *SnapshotInfo) ToCSISnapshot() *csi.Snapshot {
	return &csi.Snapshot{
		SnapshotId:     s.SnapshotID,
		SourceVolumeId: s.SourceVolumeID,
		SizeBytes:      s.SizeBytes,
		CreationTime:   timestamppb.New(s.CreatedAt),
		ReadyToUse:     s.ReadyToUse,
	}
}
