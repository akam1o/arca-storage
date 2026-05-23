package mount

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sync"

	"k8s.io/klog/v2"
	"k8s.io/mount-utils"
)

var svmNamePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*$`)

func mountLogWarning(message string, err error) {
	if err == nil {
		klog.Warning(message)
		return
	}
	klog.Warningf("%s: %T", message, err)
}

func mountLogError(message string, err error) {
	if err == nil {
		klog.Error(message)
		return
	}
	klog.Errorf("%s: %T", message, err)
}

// SVMMount represents an SVM mount point
type SVMMount struct {
	SVMName         string
	VIP             string
	ExportRoot      string
	MountPath       string
	NFSMountOptions []string
}

// MountManager manages per-SVM NFS mounts with NodeState-derived refcounting
type MountManager struct {
	mounts        map[string]*SVMMount // svmName -> mount info (in-memory only)
	nodeState     *NodeState           // Reference to NodeState for refcount derivation
	baseMountPath string               // Base path for SVM mounts
	mounter       mount.Interface
	validator     MountSourceValidator
	mu            sync.Mutex
}

// NewMountManager creates a new mount manager with NodeState reference
func NewMountManager(nodeState *NodeState, baseMountPath string) (*MountManager, error) {
	if baseMountPath == "" {
		baseMountPath = "/var/lib/kubelet/plugins/csi.arca-storage.io/mounts"
	}
	if err := validateBaseMountPath(baseMountPath); err != nil {
		return nil, err
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
		validator:     ProcMountInfoSourceValidator{},
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
	svms, err := m.nodeState.GetUniqueSVMMounts()
	if err != nil {
		return err
	}

	for svmName, info := range svms {
		if err := validateSVMName(svmName); err != nil {
			return fmt.Errorf("invalid SVM name in node state: %w", err)
		}
		mountPath := m.getMountPath(svmName)

		// Check if already mounted
		isMounted, err := m.isMountPoint(mountPath)
		if err != nil {
			mountLogWarning(fmt.Sprintf("Failed to check mount point while reconciling SVM %s", svmName), err)
			continue
		}

		if !isMounted {
			// Mount is missing - restore it
			klog.Infof("Restoring missing mount for SVM %s", svmName)
			if err := m.mountSVMLocked(svmName, info.VIP, info.ExportRoot, info.NFSMountOptions); err != nil {
				mountLogError(fmt.Sprintf("Failed to restore mount for SVM %s", svmName), err)
				// Continue with other SVMs
				continue
			}
		} else {
			if err := m.validateSVMMountSource(mountPath, svmName, info.VIP, info.ExportRoot); err != nil {
				return fmt.Errorf("existing SVM mount is not safe to reuse")
			}
			// Mount exists - record it
			m.mounts[svmName] = &SVMMount{
				SVMName:         svmName,
				VIP:             info.VIP,
				ExportRoot:      defaultExportRoot(svmName, info.ExportRoot),
				MountPath:       mountPath,
				NFSMountOptions: cloneMountOptions(info.NFSMountOptions),
			}
			klog.V(4).Infof("Found existing mount for SVM %s", svmName)
		}
	}

	klog.Infof("Reconciliation complete: %d SVM mounts restored", len(m.mounts))
	return nil
}

// EnsureSVMMount ensures an SVM is mounted (creates mount if needed)
func (m *MountManager) EnsureSVMMount(
	ctx context.Context,
	svmName,
	vip,
	exportRoot string,
	nfsMountOptions []string,
) (string, error) {
	if err := validateSVMName(svmName); err != nil {
		return "", err
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	exportRoot = defaultExportRoot(svmName, exportRoot)

	// Check if already mounted
	if mount, exists := m.mounts[svmName]; exists {
		// Verify the mount actually exists
		isMounted, err := m.isMountPoint(mount.MountPath)
		if err != nil {
			return "", fmt.Errorf("failed to check mount point: %w", err)
		}
		if isMounted {
			if err := m.validateSVMMountSource(mount.MountPath, svmName, vip, exportRoot); err != nil {
				return "", fmt.Errorf("existing SVM mount is not safe to reuse")
			}
			if mount.ExportRoot != "" && mount.ExportRoot != exportRoot {
				return "", fmt.Errorf("SVM %s already mounted with different export root", svmName)
			}
			if !sameMountOptions(mount.NFSMountOptions, nfsMountOptions) {
				return "", fmt.Errorf("SVM %s already mounted with different NFS options", svmName)
			}
			klog.V(4).Infof("SVM %s already mounted", svmName)
			return mount.MountPath, nil
		}

		// Mount record exists but actual mount is gone - need to remount
		klog.Warningf("SVM %s mount record exists but mount is gone, remounting", svmName)
		delete(m.mounts, svmName)
	}

	// Mount doesn't exist - create it
	return m.ensureSVMMountLocked(svmName, vip, exportRoot, nfsMountOptions)
}

// ensureSVMMountLocked mounts an SVM (must hold lock)
func (m *MountManager) ensureSVMMountLocked(svmName, vip, exportRoot string, nfsMountOptions []string) (string, error) {
	if err := m.mountSVMLocked(svmName, vip, exportRoot, nfsMountOptions); err != nil {
		return "", err
	}

	return m.getMountPath(svmName), nil
}

// mountSVMLocked performs the actual NFS mount (must hold lock)
func (m *MountManager) mountSVMLocked(svmName, vip, exportRoot string, nfsMountOptions []string) error {
	mountPath := m.getMountPath(svmName)
	exportRoot = defaultExportRoot(svmName, exportRoot)

	// Create mount point directory
	if err := os.MkdirAll(mountPath, 0750); err != nil {
		return fmt.Errorf("failed to create mount point: %w", err)
	}

	nfsSource := nfsSourceForSVM(vip, exportRoot)
	options := normalizeNFSMountOptions(nfsMountOptions)

	isMounted, err := m.isMountPoint(mountPath)
	if err != nil {
		return fmt.Errorf("failed to check mount point: %w", err)
	}
	if isMounted {
		if err := m.validateSVMMountSource(mountPath, svmName, vip, exportRoot); err != nil {
			return fmt.Errorf("existing SVM mount is not safe to reuse")
		}
		m.mounts[svmName] = &SVMMount{
			SVMName:         svmName,
			VIP:             vip,
			ExportRoot:      exportRoot,
			MountPath:       mountPath,
			NFSMountOptions: cloneMountOptions(options),
		}
		return nil
	}

	klog.Infof("Mounting NFS for SVM %s", svmName)

	// Perform NFS mount
	if err := m.mounter.Mount(nfsSource, mountPath, "nfs4", options); err != nil {
		return fmt.Errorf("failed to mount NFS: %w", err)
	}

	// Record mount
	m.mounts[svmName] = &SVMMount{
		SVMName:         svmName,
		VIP:             vip,
		ExportRoot:      exportRoot,
		MountPath:       mountPath,
		NFSMountOptions: cloneMountOptions(options),
	}

	klog.Infof("Successfully mounted SVM %s", svmName)
	return nil
}

// ShouldUnmountSVM checks if an SVM should be unmounted (refcount == 0)
// Refcount is derived from NodeState, not stored
func (m *MountManager) ShouldUnmountSVM(ctx context.Context, svmName string) (bool, error) {
	if err := validateSVMName(svmName); err != nil {
		return false, err
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	// Derive refcount from NodeState
	refcount, err := m.nodeState.CountStagedVolumesForSVMFresh(svmName)
	if err != nil {
		return false, fmt.Errorf("failed to refresh node state for SVM %s refcount: %w", svmName, err)
	}

	klog.V(4).Infof("SVM %s refcount (derived from NodeState): %d", svmName, refcount)

	return refcount == 0, nil
}

// UnmountSVM unmounts an SVM
func (m *MountManager) UnmountSVM(ctx context.Context, svmName string) error {
	if err := validateSVMName(svmName); err != nil {
		return err
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	mount, exists := m.mounts[svmName]
	if !exists {
		klog.V(4).Infof("SVM %s not mounted, nothing to unmount", svmName)
		return nil
	}

	// Double-check refcount before unmounting (safety check)
	refcount, err := m.nodeState.CountStagedVolumesForSVMFresh(svmName)
	if err != nil {
		return fmt.Errorf("failed to refresh node state for SVM %s refcount: %w", svmName, err)
	}
	if refcount > 0 {
		return fmt.Errorf("cannot unmount SVM %s: refcount is %d (not zero)", svmName, refcount)
	}

	klog.Infof("Unmounting SVM %s", svmName)

	// Unmount
	if err := m.mounter.Unmount(mount.MountPath); err != nil {
		return fmt.Errorf("failed to unmount SVM %s: %w", svmName, err)
	}

	// Remove mount point directory
	if err := os.Remove(mount.MountPath); err != nil {
		mountLogWarning(fmt.Sprintf("Failed to remove mount point directory for SVM %s", svmName), err)
	}

	// Remove from tracked mounts
	delete(m.mounts, svmName)

	klog.Infof("Successfully unmounted SVM %s", svmName)
	return nil
}

// GetMountPath returns the mount path for an SVM
func (m *MountManager) GetMountPath(svmName string) (string, error) {
	if err := validateSVMName(svmName); err != nil {
		return "", err
	}

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

func validateBaseMountPath(baseMountPath string) error {
	if !filepath.IsAbs(baseMountPath) {
		return fmt.Errorf("base mount path must be absolute: %s", baseMountPath)
	}
	cleaned := filepath.Clean(baseMountPath)
	if cleaned != baseMountPath {
		return fmt.Errorf("base mount path must be canonical: %s", baseMountPath)
	}
	if cleaned == string(filepath.Separator) {
		return fmt.Errorf("base mount path must not be the filesystem root")
	}
	return nil
}

func validateSVMName(svmName string) error {
	if svmName == "" {
		return fmt.Errorf("SVM name cannot be empty")
	}
	if !svmNamePattern.MatchString(svmName) {
		return fmt.Errorf("invalid SVM name %q: must start with alphanumeric and contain only alphanumeric, dots, underscores, or hyphens", svmName)
	}
	return nil
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

func (m *MountManager) validateSVMMountSource(mountPath, svmName, vip, exportRoot string) error {
	return m.mountSourceValidator().ValidateMountSource(mountPath, nfsSourceForSVM(vip, exportRoot))
}

func (m *MountManager) mountSourceValidator() MountSourceValidator {
	if m.validator != nil {
		return m.validator
	}
	return ProcMountInfoSourceValidator{}
}

func nfsSourceForSVM(vip, exportRoot string) string {
	return fmt.Sprintf("%s:%s", vip, exportRoot)
}

func defaultExportRoot(svmName, exportRoot string) string {
	if exportRoot == "" {
		return "/exports/" + svmName
	}
	return exportRoot
}
