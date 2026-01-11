package mount

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sync"

	"k8s.io/klog/v2"
	"k8s.io/mount-utils"
)

// SVMMount represents an SVM mount point
type SVMMount struct {
	SVMName   string
	VIP       string
	MountPath string
}

// MountManager manages per-SVM NFS mounts with NodeState-derived refcounting
type MountManager struct {
	mounts      map[string]*SVMMount // svmName -> mount info (in-memory only)
	nodeState   *NodeState           // Reference to NodeState for refcount derivation
	baseMountPath string              // Base path for SVM mounts
	mounter     mount.Interface
	mu          sync.Mutex
}

// NewMountManager creates a new mount manager with NodeState reference
func NewMountManager(nodeState *NodeState, baseMountPath string) (*MountManager, error) {
	if baseMountPath == "" {
		baseMountPath = "/var/lib/kubelet/plugins/csi.arca-storage.io/mounts"
	}

	// Ensure base mount directory exists
	if err := os.MkdirAll(baseMountPath, 0750); err != nil {
		return nil, fmt.Errorf("failed to create base mount directory: %w", err)
	}

	mgr := &MountManager{
		mounts:        make(map[string]*SVMMount),
		nodeState:     nodeState,
		baseMountPath: baseMountPath,
		mounter:       mount.New(""),
	}

	// Reconcile mounts from NodeState on startup
	if err := mgr.reconcile(); err != nil {
		return nil, fmt.Errorf("failed to reconcile mounts: %w", err)
	}

	return mgr, nil
}

// reconcile restores mounts based on NodeState (single source of truth)
func (m *MountManager) reconcile() error {
	m.mu.Lock()
	defer m.mu.Unlock()

	klog.Info("Reconciling SVM mounts from node state")

	// Get unique SVMs from NodeState
	svms := m.nodeState.GetUniqueSVMs()

	for svmName, vip := range svms {
		mountPath := m.getMountPath(svmName)

		// Check if already mounted
		isMounted, err := m.isMountPoint(mountPath)
		if err != nil {
			klog.Warningf("Failed to check mount point %s: %v", mountPath, err)
			continue
		}

		if !isMounted {
			// Mount is missing - restore it
			klog.Infof("Restoring missing mount for SVM %s (VIP: %s)", svmName, vip)
			if err := m.mountSVMLocked(svmName, vip); err != nil {
				klog.Errorf("Failed to restore mount for SVM %s: %v", svmName, err)
				// Continue with other SVMs
				continue
			}
		} else {
			// Mount exists - record it
			m.mounts[svmName] = &SVMMount{
				SVMName:   svmName,
				VIP:       vip,
				MountPath: mountPath,
			}
			klog.V(4).Infof("Found existing mount for SVM %s at %s", svmName, mountPath)
		}
	}

	klog.Infof("Reconciliation complete: %d SVM mounts restored", len(m.mounts))
	return nil
}

// EnsureSVMMount ensures an SVM is mounted (creates mount if needed)
func (m *MountManager) EnsureSVMMount(ctx context.Context, svmName, vip string) (string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	// Check if already mounted
	if mount, exists := m.mounts[svmName]; exists {
		// Verify the mount actually exists
		isMounted, err := m.isMountPoint(mount.MountPath)
		if err != nil {
			return "", fmt.Errorf("failed to check mount point: %w", err)
		}
		if isMounted {
			klog.V(4).Infof("SVM %s already mounted at %s", svmName, mount.MountPath)
			return mount.MountPath, nil
		}

		// Mount record exists but actual mount is gone - need to remount
		klog.Warningf("SVM %s mount record exists but mount is gone, remounting", svmName)
		delete(m.mounts, svmName)
	}

	// Mount doesn't exist - create it
	return m.ensureSVMMountLocked(svmName, vip)
}

// ensureSVMMountLocked mounts an SVM (must hold lock)
func (m *MountManager) ensureSVMMountLocked(svmName, vip string) (string, error) {
	if err := m.mountSVMLocked(svmName, vip); err != nil {
		return "", err
	}

	return m.getMountPath(svmName), nil
}

// mountSVMLocked performs the actual NFS mount (must hold lock)
func (m *MountManager) mountSVMLocked(svmName, vip string) error {
	mountPath := m.getMountPath(svmName)

	// Create mount point directory
	if err := os.MkdirAll(mountPath, 0750); err != nil {
		return fmt.Errorf("failed to create mount point: %w", err)
	}

	// NFS mount options
	nfsSource := fmt.Sprintf("%s:/exports/%s", vip, svmName)
	options := []string{
		"vers=4.2",
		"rsize=1048576",
		"wsize=1048576",
		"hard",
		"timeo=600",
		"retrans=2",
		"noresvport",
	}

	klog.Infof("Mounting NFS: %s -> %s", nfsSource, mountPath)

	// Perform NFS mount
	if err := m.mounter.Mount(nfsSource, mountPath, "nfs4", options); err != nil {
		return fmt.Errorf("failed to mount NFS: %w", err)
	}

	// Record mount
	m.mounts[svmName] = &SVMMount{
		SVMName:   svmName,
		VIP:       vip,
		MountPath: mountPath,
	}

	klog.Infof("Successfully mounted SVM %s at %s", svmName, mountPath)
	return nil
}

// ShouldUnmountSVM checks if an SVM should be unmounted (refcount == 0)
// Refcount is derived from NodeState, not stored
func (m *MountManager) ShouldUnmountSVM(ctx context.Context, svmName string) (bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	// Derive refcount from NodeState
	refcount := m.nodeState.CountStagedVolumesForSVM(svmName)

	klog.V(4).Infof("SVM %s refcount (derived from NodeState): %d", svmName, refcount)

	return refcount == 0, nil
}

// UnmountSVM unmounts an SVM
func (m *MountManager) UnmountSVM(ctx context.Context, svmName string) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	mount, exists := m.mounts[svmName]
	if !exists {
		klog.V(4).Infof("SVM %s not mounted, nothing to unmount", svmName)
		return nil
	}

	// Double-check refcount before unmounting (safety check)
	refcount := m.nodeState.CountStagedVolumesForSVM(svmName)
	if refcount > 0 {
		return fmt.Errorf("cannot unmount SVM %s: refcount is %d (not zero)", svmName, refcount)
	}

	klog.Infof("Unmounting SVM %s from %s", svmName, mount.MountPath)

	// Unmount
	if err := m.mounter.Unmount(mount.MountPath); err != nil {
		return fmt.Errorf("failed to unmount SVM %s: %w", svmName, err)
	}

	// Remove mount point directory
	if err := os.Remove(mount.MountPath); err != nil {
		klog.Warningf("Failed to remove mount point directory %s: %v", mount.MountPath, err)
	}

	// Remove from tracked mounts
	delete(m.mounts, svmName)

	klog.Infof("Successfully unmounted SVM %s", svmName)
	return nil
}

// GetMountPath returns the mount path for an SVM
func (m *MountManager) GetMountPath(svmName string) (string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	mount, exists := m.mounts[svmName]
	if !exists {
		return "", fmt.Errorf("SVM %s is not mounted", svmName)
	}

	return mount.MountPath, nil
}

// getMountPath constructs the mount path for an SVM (must hold lock or be in init)
func (m *MountManager) getMountPath(svmName string) string {
	return filepath.Join(m.baseMountPath, svmName)
}

// isMountPoint checks if a path is a mount point
func (m *MountManager) isMountPoint(path string) (bool, error) {
	notMnt, err := m.mounter.IsLikelyNotMountPoint(path)
	if err != nil {
		if os.IsNotExist(err) {
			return false, nil
		}
		return false, err
	}
	return !notMnt, nil
}

// IsMountPoint checks if a path is a mount point (public wrapper)
func (m *MountManager) IsMountPoint(path string) (bool, error) {
	return m.isMountPoint(path)
}
