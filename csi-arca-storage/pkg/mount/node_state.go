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
	VolumeID          string          `json:"volume_id"`
	SVMName           string          `json:"svm_name"`
	VIP               string          `json:"vip"`
	ExportRoot        string          `json:"export_root,omitempty"`
	VolumePath        string          `json:"volume_path,omitempty"`
	StagingPath       string          `json:"staging_path"`
	NFSMountOptions   []string        `json:"nfs_mount_options,omitempty"`
	PublishedPaths    []string        `json:"published_paths"`              // Target paths where volume is published
	PublishedReadOnly map[string]bool `json:"published_readonly,omitempty"` // target path -> readonly request state
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

// SVMMountInfo represents the information needed to restore an SVM mount.
type SVMMountInfo struct {
	VIP             string
	ExportRoot      string
	NFSMountOptions []string
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
func (ns *NodeState) RecordVolumeStaging(
	volumeID,
	svmName,
	vip,
	exportRoot,
	volumePath,
	stagingPath string,
	nfsMountOptions []string,
) error {
	ns.mu.Lock()
	defer ns.mu.Unlock()

	ns.data.Volumes[volumeID] = &VolumeStaging{
		VolumeID:        volumeID,
		SVMName:         svmName,
		VIP:             vip,
		ExportRoot:      defaultExportRoot(svmName, exportRoot),
		VolumePath:      volumePath,
		StagingPath:     stagingPath,
		NFSMountOptions: append([]string(nil), nfsMountOptions...),
	}

	return ns.persistLocked()
}

// ValidateVolumeStaging verifies that an existing staged mount belongs to the
// requested volume before treating NodeStageVolume as idempotent.
func (ns *NodeState) ValidateVolumeStaging(
	volumeID,
	svmName,
	vip,
	exportRoot,
	volumePath,
	stagingPath string,
	nfsMountOptions []string,
) error {
	ns.mu.RLock()
	defer ns.mu.RUnlock()

	staging, exists := ns.data.Volumes[volumeID]
	if !exists {
		return fmt.Errorf("volume %s is mounted at %s but is not recorded in node state", volumeID, stagingPath)
	}
	if staging.SVMName != svmName {
		return fmt.Errorf("volume %s SVM mismatch: recorded=%s requested=%s", volumeID, staging.SVMName, svmName)
	}
	if staging.VIP != vip {
		return fmt.Errorf("volume %s VIP mismatch: recorded=%s requested=%s", volumeID, staging.VIP, vip)
	}
	if defaultExportRoot(svmName, staging.ExportRoot) != defaultExportRoot(svmName, exportRoot) {
		return fmt.Errorf(
			"volume %s export root mismatch: recorded=%s requested=%s",
			volumeID,
			defaultExportRoot(svmName, staging.ExportRoot),
			defaultExportRoot(svmName, exportRoot),
		)
	}
	if staging.VolumePath != "" && staging.VolumePath != volumePath {
		return fmt.Errorf("volume %s path mismatch: recorded=%s requested=%s", volumeID, staging.VolumePath, volumePath)
	}
	if staging.StagingPath != stagingPath {
		return fmt.Errorf("volume %s staging path mismatch: recorded=%s requested=%s", volumeID, staging.StagingPath, stagingPath)
	}
	if !sameMountOptions(staging.NFSMountOptions, nfsMountOptions) {
		return fmt.Errorf(
			"volume %s NFS options mismatch: recorded=%v requested=%v",
			volumeID,
			normalizeNFSMountOptions(staging.NFSMountOptions),
			normalizeNFSMountOptions(nfsMountOptions),
		)
	}
	return nil
}

// ValidateVolumeStagingPath verifies that volumeID is staged at stagingPath.
func (ns *NodeState) ValidateVolumeStagingPath(volumeID, stagingPath string) error {
	ns.mu.RLock()
	defer ns.mu.RUnlock()

	staging, exists := ns.data.Volumes[volumeID]
	if !exists {
		return fmt.Errorf("volume %s is not staged in node state", volumeID)
	}
	if staging.StagingPath != stagingPath {
		return fmt.Errorf("volume %s staging path mismatch: recorded=%s requested=%s", volumeID, staging.StagingPath, stagingPath)
	}
	return nil
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
		staging.NFSMountOptions = cloneMountOptions(v.NFSMountOptions)
		staging.PublishedPaths = cloneMountOptions(v.PublishedPaths)
		staging.PublishedReadOnly = cloneBoolMap(v.PublishedReadOnly)
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

// GetUniqueSVMMounts returns one restore record per SVM from staged volumes.
func (ns *NodeState) GetUniqueSVMMounts() (map[string]SVMMountInfo, error) {
	ns.mu.RLock()
	defer ns.mu.RUnlock()

	svms := make(map[string]SVMMountInfo)
	for _, staging := range ns.data.Volumes {
		options := normalizeNFSMountOptions(staging.NFSMountOptions)
		if existing, exists := svms[staging.SVMName]; exists {
			if existing.VIP != staging.VIP {
				return nil, fmt.Errorf(
					"conflicting VIPs for SVM %s in node state: existing=%s requested=%s",
					staging.SVMName,
					existing.VIP,
					staging.VIP,
				)
			}
			if defaultExportRoot(staging.SVMName, existing.ExportRoot) != defaultExportRoot(staging.SVMName, staging.ExportRoot) {
				return nil, fmt.Errorf(
					"conflicting export roots for SVM %s in node state: existing=%s requested=%s",
					staging.SVMName,
					defaultExportRoot(staging.SVMName, existing.ExportRoot),
					defaultExportRoot(staging.SVMName, staging.ExportRoot),
				)
			}
			if !sameMountOptions(existing.NFSMountOptions, options) {
				return nil, fmt.Errorf(
					"conflicting NFS mount options for SVM %s in node state: existing=%v requested=%v",
					staging.SVMName,
					normalizeNFSMountOptions(existing.NFSMountOptions),
					options,
				)
			}
			continue
		}
		svms[staging.SVMName] = SVMMountInfo{
			VIP:             staging.VIP,
			ExportRoot:      defaultExportRoot(staging.SVMName, staging.ExportRoot),
			NFSMountOptions: cloneMountOptions(options),
		}
	}

	return svms, nil
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
		if closeErr := f.Close(); closeErr != nil {
			removeTempStateFile(tempPath)
			return fmt.Errorf("failed to write temp file: %w; also failed to close temp file: %v", err, closeErr)
		}
		removeTempStateFile(tempPath)
		return fmt.Errorf("failed to write temp file: %w", err)
	}

	// Fsync to ensure data is on disk
	if err := f.Sync(); err != nil {
		if closeErr := f.Close(); closeErr != nil {
			removeTempStateFile(tempPath)
			return fmt.Errorf("failed to fsync temp file: %w; also failed to close temp file: %v", err, closeErr)
		}
		removeTempStateFile(tempPath)
		return fmt.Errorf("failed to fsync temp file: %w", err)
	}

	if err := f.Close(); err != nil {
		removeTempStateFile(tempPath)
		return fmt.Errorf("failed to close temp file: %w", err)
	}

	// Atomic rename
	if err := os.Rename(tempPath, ns.stateFilePath); err != nil {
		removeTempStateFile(tempPath)
		return fmt.Errorf("failed to rename temp file: %w", err)
	}

	// Fsync directory to ensure rename is persisted
	dir, err := os.Open(filepath.Dir(ns.stateFilePath))
	if err != nil {
		return fmt.Errorf("failed to open state directory for fsync: %w", err)
	}
	if err := dir.Sync(); err != nil {
		if closeErr := dir.Close(); closeErr != nil {
			return fmt.Errorf("failed to fsync state directory: %w; also failed to close state directory: %v", err, closeErr)
		}
		return fmt.Errorf("failed to fsync state directory: %w", err)
	}
	if err := dir.Close(); err != nil {
		return fmt.Errorf("failed to close state directory: %w", err)
	}

	klog.V(4).Infof("Persisted node state with %d volumes", len(ns.data.Volumes))

	return nil
}

func removeTempStateFile(tempPath string) {
	if err := os.Remove(tempPath); err != nil && !os.IsNotExist(err) {
		klog.Warningf("Failed to remove temp state file %s: %v", tempPath, err)
	}
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

// RecordVolumePublish records that a volume has been published to a target path.
func (ns *NodeState) RecordVolumePublish(volumeID, targetPath string, readOnly bool) error {
	ns.mu.Lock()
	defer ns.mu.Unlock()

	staging, exists := ns.data.Volumes[volumeID]
	if !exists {
		return fmt.Errorf("volume %s not found in node state", volumeID)
	}
	if staging.PublishedReadOnly == nil {
		staging.PublishedReadOnly = make(map[string]bool)
	}

	// Check if already published to this path
	for _, path := range staging.PublishedPaths {
		if path == targetPath {
			if recordedReadOnly, recorded := staging.PublishedReadOnly[targetPath]; recorded && recordedReadOnly != readOnly {
				return fmt.Errorf(
					"volume %s publish readonly mismatch for %s: recorded=%t requested=%t",
					volumeID,
					targetPath,
					recordedReadOnly,
					readOnly,
				)
			}
			if _, recorded := staging.PublishedReadOnly[targetPath]; !recorded {
				staging.PublishedReadOnly[targetPath] = readOnly
				if err := ns.persistLocked(); err != nil {
					return fmt.Errorf("failed to persist state: %w", err)
				}
			}
			klog.V(4).Infof("Volume %s already published to %s", volumeID, targetPath)
			return nil
		}
	}

	// Add target path
	staging.PublishedPaths = append(staging.PublishedPaths, targetPath)
	staging.PublishedReadOnly[targetPath] = readOnly

	// Persist updated state
	if err := ns.persistLocked(); err != nil {
		return fmt.Errorf("failed to persist state: %w", err)
	}

	klog.V(4).Infof("Recorded volume %s publish to %s", volumeID, targetPath)
	return nil
}

// HasVolumePublish returns true when targetPath is recorded for volumeID.
func (ns *NodeState) HasVolumePublish(volumeID, targetPath string) bool {
	ns.mu.RLock()
	defer ns.mu.RUnlock()

	staging, exists := ns.data.Volumes[volumeID]
	if !exists {
		return false
	}
	for _, path := range staging.PublishedPaths {
		if path == targetPath {
			return true
		}
	}
	return false
}

// ValidateVolumePublish verifies that targetPath is recorded with the requested readonly state.
func (ns *NodeState) ValidateVolumePublish(volumeID, targetPath string, readOnly bool) error {
	ns.mu.RLock()
	defer ns.mu.RUnlock()

	staging, exists := ns.data.Volumes[volumeID]
	if !exists {
		return fmt.Errorf("target path %s is not recorded for volume %s", targetPath, volumeID)
	}
	for _, path := range staging.PublishedPaths {
		if path != targetPath {
			continue
		}
		if staging.PublishedReadOnly != nil {
			if recordedReadOnly, recorded := staging.PublishedReadOnly[targetPath]; recorded && recordedReadOnly != readOnly {
				return fmt.Errorf(
					"volume %s publish readonly mismatch for %s: recorded=%t requested=%t",
					volumeID,
					targetPath,
					recordedReadOnly,
					readOnly,
				)
			}
		}
		return nil
	}
	return fmt.Errorf("target path %s is not recorded for volume %s", targetPath, volumeID)
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
	if staging.PublishedReadOnly != nil {
		delete(staging.PublishedReadOnly, targetPath)
	}

	// Persist updated state
	if err := ns.persistLocked(); err != nil {
		return fmt.Errorf("failed to persist state: %w", err)
	}

	klog.V(4).Infof("Removed volume %s publish from %s", volumeID, targetPath)
	return nil
}
