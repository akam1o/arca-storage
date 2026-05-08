package store

import (
	"fmt"
	"sort"
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
	ExportRoot    string
	Path          string
	CapacityBytes int64
	CreatedAt     time.Time
	ContentSource *csi.VolumeContentSource
	ReadyToUse    *bool

	TemporaryCloneSnapshot         string
	TemporaryCloneSourceVolumePath string
}

// SnapshotInfo represents snapshot metadata
type SnapshotInfo struct {
	SnapshotID       string
	Name             string // Original VolumeSnapshot name
	SourceVolumeID   string
	SourceVolumePath string
	SVMName          string
	Path             string
	SizeBytes        int64
	CreatedAt        time.Time
	ReadyToUse       bool
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
	s.volumes[info.VolumeID] = deepCopyVolumeInfo(info)
	return nil
}

// UpdateVolume updates existing volume metadata
func (s *MemoryStore) UpdateVolume(info *VolumeInfo) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	existing, exists := s.volumes[info.VolumeID]
	if !exists {
		return fmt.Errorf("%w: volume %s", ErrNotFound, info.VolumeID)
	}

	updated := deepCopyVolumeInfo(info)
	if updated.CapacityBytes < existing.CapacityBytes {
		updated.CapacityBytes = existing.CapacityBytes
	}
	s.volumes[info.VolumeID] = updated
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

	return deepCopyVolumeInfo(info), nil
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

	volumeIDs := make([]string, 0, len(s.volumes))
	for volumeID := range s.volumes {
		volumeIDs = append(volumeIDs, volumeID)
	}
	sort.Strings(volumeIDs)

	start := 0
	if startingToken != "" {
		idx := sort.SearchStrings(volumeIDs, startingToken)
		if idx == len(volumeIDs) || volumeIDs[idx] != startingToken {
			return nil, "", fmt.Errorf("%w: volume pagination token %s", ErrNotFound, startingToken)
		}
		start = idx + 1
	}

	end := len(volumeIDs)
	if maxEntries > 0 && start+maxEntries < end {
		end = start + maxEntries
		nextToken = volumeIDs[end-1]
	}

	for _, volumeID := range volumeIDs[start:end] {
		result = append(result, deepCopyVolumeInfo(s.volumes[volumeID]))
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
	s.snapshots[info.SnapshotID] = deepCopySnapshotInfo(info)
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

	return deepCopySnapshotInfo(info), nil
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

	snapshotIDs := make([]string, 0, len(s.snapshots))
	for snapshotID, info := range s.snapshots {
		if sourceVolumeID != "" && info.SourceVolumeID != sourceVolumeID {
			continue
		}
		snapshotIDs = append(snapshotIDs, snapshotID)
	}
	sort.Strings(snapshotIDs)

	start := 0
	if startingToken != "" {
		idx := sort.SearchStrings(snapshotIDs, startingToken)
		if idx == len(snapshotIDs) || snapshotIDs[idx] != startingToken {
			return nil, "", fmt.Errorf("%w: snapshot pagination token %s", ErrNotFound, startingToken)
		}
		start = idx + 1
	}

	end := len(snapshotIDs)
	if maxEntries > 0 && start+maxEntries < end {
		end = start + maxEntries
		nextToken = snapshotIDs[end-1]
	}

	for _, snapshotID := range snapshotIDs[start:end] {
		result = append(result, deepCopySnapshotInfo(s.snapshots[snapshotID]))
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
			"exportRoot": defaultExportRoot(v.SVMName, v.ExportRoot),
			"volumePath": v.Path,
		},
		ContentSource: v.ContentSource,
	}
}

func defaultExportRoot(svmName, exportRoot string) string {
	if exportRoot == "" {
		return "/exports/" + svmName
	}
	return exportRoot
}

// VolumeReadyState returns a pointer so nil can mean "legacy volume, ready".
func VolumeReadyState(ready bool) *bool {
	return &ready
}

// IsVolumeReady treats missing readiness as ready for backward compatibility.
func IsVolumeReady(info *VolumeInfo) bool {
	return info != nil && (info.ReadyToUse == nil || *info.ReadyToUse)
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
