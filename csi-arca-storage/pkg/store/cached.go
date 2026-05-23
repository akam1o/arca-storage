// SPDX-License-Identifier: Apache-2.0

package store

import (
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/container-storage-interface/spec/lib/go/csi"
	lru "github.com/hashicorp/golang-lru/v2"
	"google.golang.org/protobuf/proto"
	"k8s.io/klog/v2"
)

// cacheEntry wraps cached data with timestamp for TTL checking
type cacheEntry struct {
	data      interface{}
	timestamp time.Time
}

// CachedStore wraps a Store implementation with an LRU cache
type CachedStore struct {
	store         Store
	volumeCache   *lru.Cache[string, *cacheEntry]
	snapshotCache *lru.Cache[string, *cacheEntry]
	cacheTTL      time.Duration
	mu            sync.Mutex // Use exclusive Mutex for all LRU operations (thread-safe)
}

// NewCachedStore creates a new cached store wrapper
func NewCachedStore(store Store, cacheTTL time.Duration, volumeCacheSize, snapshotCacheSize int) (*CachedStore, error) {
	volumeCache, err := lru.New[string, *cacheEntry](volumeCacheSize)
	if err != nil {
		return nil, fmt.Errorf("failed to create volume cache: %w", err)
	}

	snapshotCache, err := lru.New[string, *cacheEntry](snapshotCacheSize)
	if err != nil {
		return nil, fmt.Errorf("failed to create snapshot cache: %w", err)
	}

	klog.Infof("Initialized cache: volumeSize=%d, snapshotSize=%d, TTL=%v", volumeCacheSize, snapshotCacheSize, cacheTTL)

	return &CachedStore{
		store:         store,
		volumeCache:   volumeCache,
		snapshotCache: snapshotCache,
		cacheTTL:      cacheTTL,
	}, nil
}

// isExpired checks if a cache entry has exceeded TTL
func (s *CachedStore) isExpired(entry *cacheEntry) bool {
	return time.Since(entry.timestamp) > s.cacheTTL
}

func cloneVolumeContentSource(source *csi.VolumeContentSource) *csi.VolumeContentSource {
	if source == nil {
		return nil
	}
	return proto.Clone(source).(*csi.VolumeContentSource)
}

// deepCopyVolumeInfo creates a deep copy to prevent mutation issues
func deepCopyVolumeInfo(v *VolumeInfo) *VolumeInfo {
	if v == nil {
		return nil
	}
	copied := *v
	copied.ContentSource = cloneVolumeContentSource(v.ContentSource)
	if v.ReadyToUse != nil {
		ready := *v.ReadyToUse
		copied.ReadyToUse = &ready
	}
	return &copied
}

// deepCopySnapshotInfo creates a deep copy to prevent mutation issues
func deepCopySnapshotInfo(s *SnapshotInfo) *SnapshotInfo {
	if s == nil {
		return nil
	}
	copied := *s
	return &copied
}

// CreateVolume creates a volume and invalidates cache
func (s *CachedStore) CreateVolume(ctx context.Context, info *VolumeInfo) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	err := s.store.CreateVolume(ctx, info)
	if err != nil {
		return err
	}

	// Invalidate cache for this volume
	s.mu.Lock()
	s.volumeCache.Remove(info.VolumeID)
	s.mu.Unlock()

	return nil
}

// UpdateVolume updates a volume and invalidates cache
func (s *CachedStore) UpdateVolume(ctx context.Context, info *VolumeInfo) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	err := s.store.UpdateVolume(ctx, info)
	if err != nil {
		return err
	}

	// Invalidate cache for this volume
	s.mu.Lock()
	s.volumeCache.Remove(info.VolumeID)
	s.mu.Unlock()

	return nil
}

// GetVolume retrieves a volume, using cache when possible
func (s *CachedStore) GetVolume(ctx context.Context, volumeID string) (*VolumeInfo, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	// Check cache first (with exclusive lock for LRU safety)
	s.mu.Lock()
	entry, ok := s.volumeCache.Get(volumeID)
	if ok && !s.isExpired(entry) {
		s.mu.Unlock()
		klog.V(4).Infof("Volume cache hit: %s", volumeID)
		// Return a deep copy to prevent mutation
		return deepCopyVolumeInfo(entry.data.(*VolumeInfo)), nil
	}
	s.mu.Unlock()

	// Cache miss or expired - fetch from store
	klog.V(4).Infof("Volume cache miss: %s", volumeID)
	info, err := s.store.GetVolume(ctx, volumeID)
	if err != nil {
		return nil, err
	}

	// Populate cache (store a copy to prevent mutation)
	s.mu.Lock()
	s.volumeCache.Add(volumeID, &cacheEntry{
		data:      deepCopyVolumeInfo(info),
		timestamp: time.Now(),
	})
	s.mu.Unlock()

	// Return a deep copy to the caller
	return deepCopyVolumeInfo(info), nil
}

// DeleteVolume deletes a volume and invalidates cache
func (s *CachedStore) DeleteVolume(ctx context.Context, volumeID string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	err := s.store.DeleteVolume(ctx, volumeID)
	if err != nil {
		return err
	}

	// Invalidate cache
	s.mu.Lock()
	s.volumeCache.Remove(volumeID)
	s.mu.Unlock()

	return nil
}

// ListVolumes returns all volumes (no caching for list operations)
func (s *CachedStore) ListVolumes(ctx context.Context, startingToken string, maxEntries int) ([]*VolumeInfo, string, error) {
	if err := ctx.Err(); err != nil {
		return nil, "", err
	}
	return s.store.ListVolumes(ctx, startingToken, maxEntries)
}

// CreateSnapshot creates a snapshot and invalidates cache
func (s *CachedStore) CreateSnapshot(ctx context.Context, info *SnapshotInfo) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	err := s.store.CreateSnapshot(ctx, info)
	if err != nil {
		return err
	}

	// Invalidate cache for this snapshot
	s.mu.Lock()
	s.snapshotCache.Remove(info.SnapshotID)
	s.mu.Unlock()

	return nil
}

// UpdateSnapshotStatus updates snapshot status and invalidates cache
func (s *CachedStore) UpdateSnapshotStatus(ctx context.Context, snapshotID string, readyToUse bool) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	// Update in backing store first
	if err := s.store.UpdateSnapshotStatus(ctx, snapshotID, readyToUse); err != nil {
		return err
	}

	// Invalidate cache entry (status changed)
	s.mu.Lock()
	s.snapshotCache.Remove(snapshotID)
	s.mu.Unlock()

	return nil
}

// GetSnapshot retrieves a snapshot, using cache when possible
func (s *CachedStore) GetSnapshot(ctx context.Context, snapshotID string) (*SnapshotInfo, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	// Check cache first (with exclusive lock for LRU safety)
	s.mu.Lock()
	entry, ok := s.snapshotCache.Get(snapshotID)
	if ok && !s.isExpired(entry) {
		s.mu.Unlock()
		klog.V(4).Infof("Snapshot cache hit: %s", snapshotID)
		// Return a deep copy to prevent mutation
		return deepCopySnapshotInfo(entry.data.(*SnapshotInfo)), nil
	}
	s.mu.Unlock()

	// Cache miss or expired - fetch from store
	klog.V(4).Infof("Snapshot cache miss: %s", snapshotID)
	info, err := s.store.GetSnapshot(ctx, snapshotID)
	if err != nil {
		return nil, err
	}

	// Populate cache (store a copy to prevent mutation)
	s.mu.Lock()
	s.snapshotCache.Add(snapshotID, &cacheEntry{
		data:      deepCopySnapshotInfo(info),
		timestamp: time.Now(),
	})
	s.mu.Unlock()

	// Return a deep copy to the caller
	return deepCopySnapshotInfo(info), nil
}

// DeleteSnapshot deletes a snapshot and invalidates cache
func (s *CachedStore) DeleteSnapshot(ctx context.Context, snapshotID string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	err := s.store.DeleteSnapshot(ctx, snapshotID)
	if err != nil {
		return err
	}

	// Invalidate cache
	s.mu.Lock()
	s.snapshotCache.Remove(snapshotID)
	s.mu.Unlock()

	return nil
}

// ListSnapshots returns all snapshots (no caching for list operations)
func (s *CachedStore) ListSnapshots(ctx context.Context, sourceVolumeID, startingToken string, maxEntries int) ([]*SnapshotInfo, string, error) {
	if err := ctx.Err(); err != nil {
		return nil, "", err
	}
	return s.store.ListSnapshots(ctx, sourceVolumeID, startingToken, maxEntries)
}
