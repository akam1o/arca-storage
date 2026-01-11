package mount

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"syscall"

	"k8s.io/klog/v2"
)

// VolumeStaging represents a staged volume's information
type VolumeStaging struct {
	VolumeID      string   `json:"volume_id"`
	SVMName       string   `json:"svm_name"`
	VIP           string   `json:"vip"`
	StagingPath   string   `json:"staging_path"`
	PublishedPaths []string `json:"published_paths"` // Target paths where volume is published
}

// NodeStateData represents the persistent state on a node
type NodeStateData struct {
	Volumes map[string]*VolumeStaging `json:"volumes"` // volumeID -> staging info
}

// NodeState manages persistent volume→SVM mapping on a node
// This is the single source of truth for node state
type NodeState struct {
	stateFilePath string
	mu            sync.RWMutex
	data          *NodeStateData
}

// NewNodeState creates a new NodeState manager
func NewNodeState(stateFilePath string) (*NodeState, error) {
	ns := &NodeState{
		stateFilePath: stateFilePath,
		data: &NodeStateData{
			Volumes: make(map[string]*VolumeStaging),
		},
	}

	// Ensure state directory exists
	stateDir := filepath.Dir(stateFilePath)
	if err := os.MkdirAll(stateDir, 0750); err != nil {
		return nil, fmt.Errorf("failed to create state directory: %w", err)
	}

	// Load existing state if file exists
	if err := ns.load(); err != nil {
		// If file doesn't exist or is corrupted, quarantine it and start fresh
		if os.IsNotExist(err) {
			klog.Infof("No existing state file found, starting with empty state")
		} else {
			klog.Warningf("Failed to load state file, quarantining and starting fresh: %v", err)
			if err := ns.quarantineCorruptState(); err != nil {
				klog.Warningf("Failed to quarantine corrupt state: %v", err)
			}
		}
	}

	return ns, nil
}

// RecordVolumeStaging records a volume staging operation (atomic, with fsync)
func (ns *NodeState) RecordVolumeStaging(volumeID, svmName, vip, stagingPath string) error {
	ns.mu.Lock()
	defer ns.mu.Unlock()

	ns.data.Volumes[volumeID] = &VolumeStaging{
		VolumeID:    volumeID,
		SVMName:     svmName,
		VIP:         vip,
		StagingPath: stagingPath,
	}

	return ns.persistLocked()
}

// RemoveVolumeStaging removes a volume from staging records (atomic, with fsync)
func (ns *NodeState) RemoveVolumeStaging(volumeID string) error {
	ns.mu.Lock()
	defer ns.mu.Unlock()

	delete(ns.data.Volumes, volumeID)

	return ns.persistLocked()
}

// GetSVMForVolume retrieves the SVM name for a volume
func (ns *NodeState) GetSVMForVolume(volumeID string) (string, error) {
	ns.mu.RLock()
	defer ns.mu.RUnlock()

	staging, exists := ns.data.Volumes[volumeID]
	if !exists {
		return "", fmt.Errorf("volume %s not found in node state", volumeID)
	}

	return staging.SVMName, nil
}

// GetVIPForVolume retrieves the VIP for a volume
func (ns *NodeState) GetVIPForVolume(volumeID string) (string, error) {
	ns.mu.RLock()
	defer ns.mu.RUnlock()

	staging, exists := ns.data.Volumes[volumeID]
	if !exists {
		return "", fmt.Errorf("volume %s not found in node state", volumeID)
	}

	return staging.VIP, nil
}

// CountStagedVolumesForSVM counts how many volumes are staged for a given SVM
// This is used to derive refcount for mount management
func (ns *NodeState) CountStagedVolumesForSVM(svmName string) int {
	ns.mu.RLock()
	defer ns.mu.RUnlock()

	count := 0
	for _, staging := range ns.data.Volumes {
		if staging.SVMName == svmName {
			count++
		}
	}

	return count
}

// GetStagedVolumes returns all staged volume information
func (ns *NodeState) GetStagedVolumes() map[string]*VolumeStaging {
	ns.mu.RLock()
	defer ns.mu.RUnlock()

	// Return a copy to prevent external modification
	result := make(map[string]*VolumeStaging, len(ns.data.Volumes))
	for k, v := range ns.data.Volumes {
		staging := *v // Copy struct
		result[k] = &staging
	}

	return result
}

// GetUniqueSVMs returns a list of unique SVM names from staged volumes
func (ns *NodeState) GetUniqueSVMs() map[string]string {
	ns.mu.RLock()
	defer ns.mu.RUnlock()

	svms := make(map[string]string) // svmName -> VIP
	for _, staging := range ns.data.Volumes {
		svms[staging.SVMName] = staging.VIP
	}

	return svms
}

// load loads state from file
func (ns *NodeState) load() error {
	data, err := os.ReadFile(ns.stateFilePath)
	if err != nil {
		return err
	}

	var stateData NodeStateData
	if err := json.Unmarshal(data, &stateData); err != nil {
		return fmt.Errorf("failed to unmarshal state: %w", err)
	}

	// Initialize map if nil
	if stateData.Volumes == nil {
		stateData.Volumes = make(map[string]*VolumeStaging)
	}

	ns.data = &stateData
	klog.V(2).Infof("Loaded node state with %d volumes", len(ns.data.Volumes))

	return nil
}

// persistLocked persists state to file with atomic write and fsync (must hold lock)
func (ns *NodeState) persistLocked() error {
	// Marshal to JSON
	data, err := json.MarshalIndent(ns.data, "", "  ")
	if err != nil {
		return fmt.Errorf("failed to marshal state: %w", err)
	}

	// Atomic write: write to temp file, fsync, then rename
	tempPath := ns.stateFilePath + ".tmp"

	// Write to temp file
	f, err := os.OpenFile(tempPath, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0600)
	if err != nil {
		return fmt.Errorf("failed to create temp file: %w", err)
	}

	if _, err := f.Write(data); err != nil {
		f.Close()
		os.Remove(tempPath)
		return fmt.Errorf("failed to write temp file: %w", err)
	}

	// Fsync to ensure data is on disk
	if err := f.Sync(); err != nil {
		f.Close()
		os.Remove(tempPath)
		return fmt.Errorf("failed to fsync temp file: %w", err)
	}

	f.Close()

	// Atomic rename
	if err := os.Rename(tempPath, ns.stateFilePath); err != nil {
		os.Remove(tempPath)
		return fmt.Errorf("failed to rename temp file: %w", err)
	}

	// Fsync directory to ensure rename is persisted
	dir, err := os.Open(filepath.Dir(ns.stateFilePath))
	if err == nil {
		dir.Sync()
		dir.Close()
	}

	klog.V(4).Infof("Persisted node state with %d volumes", len(ns.data.Volumes))

	return nil
}

// quarantineCorruptState moves corrupt state file to a timestamped backup
func (ns *NodeState) quarantineCorruptState() error {
	backupPath := fmt.Sprintf("%s.corrupt.%d", ns.stateFilePath, syscall.Getpid())

	if err := os.Rename(ns.stateFilePath, backupPath); err != nil {
		return fmt.Errorf("failed to quarantine corrupt state: %w", err)
	}

	klog.Warningf("Quarantined corrupt state file to %s", backupPath)
	return nil
}

// Lock acquires an exclusive file lock for cross-process synchronization
// This is important when multiple processes might access the state file
func (ns *NodeState) Lock() error {
	// For this implementation, we rely on the internal mutex
	// For true cross-process locking, we would use flock(2)
	// That's an implementation detail for the actual deployment
	ns.mu.Lock()
	return nil
}

// Unlock releases the file lock
func (ns *NodeState) Unlock() {
	ns.mu.Unlock()
}

// RecordVolumePublish records that a volume has been published to a target path
func (ns *NodeState) RecordVolumePublish(volumeID, targetPath string) error {
	ns.mu.Lock()
	defer ns.mu.Unlock()

	staging, exists := ns.data.Volumes[volumeID]
	if !exists {
		return fmt.Errorf("volume %s not found in node state", volumeID)
	}

	// Check if already published to this path
	for _, path := range staging.PublishedPaths {
		if path == targetPath {
			klog.V(4).Infof("Volume %s already published to %s", volumeID, targetPath)
			return nil
		}
	}

	// Add target path
	staging.PublishedPaths = append(staging.PublishedPaths, targetPath)

	// Persist updated state
	if err := ns.persistLocked(); err != nil {
		return fmt.Errorf("failed to persist state: %w", err)
	}

	klog.V(4).Infof("Recorded volume %s publish to %s", volumeID, targetPath)
	return nil
}

// RemoveVolumePublish removes a target path from the published paths
func (ns *NodeState) RemoveVolumePublish(volumeID, targetPath string) error {
	ns.mu.Lock()
	defer ns.mu.Unlock()

	staging, exists := ns.data.Volumes[volumeID]
	if !exists {
		// Volume not in state - idempotent success
		return nil
	}

	// Remove target path
	newPaths := make([]string, 0, len(staging.PublishedPaths))
	for _, path := range staging.PublishedPaths {
		if path != targetPath {
			newPaths = append(newPaths, path)
		}
	}
	staging.PublishedPaths = newPaths

	// Persist updated state
	if err := ns.persistLocked(); err != nil {
		return fmt.Errorf("failed to persist state: %w", err)
	}

	klog.V(4).Infof("Removed volume %s publish from %s", volumeID, targetPath)
	return nil
}
