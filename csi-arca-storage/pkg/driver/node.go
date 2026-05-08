package driver

import (
	"context"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"

	"github.com/container-storage-interface/spec/lib/go/csi"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"k8s.io/klog/v2"
	mountutils "k8s.io/mount-utils"

	arcamount "github.com/akam1o/csi-arca-storage/pkg/mount"
)

var svmNamePattern = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9._-]*$`)

func (d *Driver) ensureNodeServiceConfigured() error {
	if d.mode != "node" {
		return status.Errorf(codes.FailedPrecondition, "node service is not available in %s mode", d.mode)
	}
	if d.nodeID == "" || d.nodeState == nil || d.mountManager == nil {
		return status.Error(codes.FailedPrecondition, "node service is not configured (run as node plugin with node-id)")
	}
	return nil
}

// validateSVMName validates SVM names sourced from CSI volume context.
func validateSVMName(name string) error {
	if name == "" {
		return fmt.Errorf("SVM name cannot be empty")
	}
	if !svmNamePattern.MatchString(name) {
		return fmt.Errorf("invalid SVM name %q: must start with alphanumeric and contain only alphanumeric, dots, underscores, or hyphens", name)
	}
	return nil
}

// validateVolumePath validates that a volume path doesn't contain path traversal patterns
func validateVolumePath(path string) error {
	// Reject empty paths
	if path == "" {
		return fmt.Errorf("volume path cannot be empty")
	}

	// Reject absolute paths (should be relative to SVM root)
	if filepath.IsAbs(path) {
		return fmt.Errorf("volume path must be relative, not absolute: %s", path)
	}

	// Clean the path and check for traversal attempts
	cleaned := filepath.Clean(path)
	if cleaned == "." {
		return fmt.Errorf("volume path must identify a directory below the SVM root: %s", path)
	}
	if cleaned != path {
		return fmt.Errorf("volume path must be canonical: %s", path)
	}
	for _, part := range strings.Split(cleaned, string(filepath.Separator)) {
		if part == "" || part == "." || part == ".." {
			return fmt.Errorf("volume path contains invalid path segment %q: %s", part, path)
		}
	}

	return nil
}

// validateVIP validates that a VIP is a valid IP address
func validateVIP(vip string) error {
	if vip == "" {
		return fmt.Errorf("VIP cannot be empty")
	}

	// Parse as IP address
	ip := net.ParseIP(vip)
	if ip == nil {
		return fmt.Errorf("invalid VIP address: %s", vip)
	}

	return nil
}

func defaultExportRoot(svmName, exportRoot string) string {
	if exportRoot == "" {
		return "/exports/" + svmName
	}
	return exportRoot
}

func validateExportRoot(exportRoot string) error {
	if exportRoot == "" {
		return fmt.Errorf("export root cannot be empty")
	}
	if !filepath.IsAbs(exportRoot) {
		return fmt.Errorf("export root must be absolute: %s", exportRoot)
	}
	cleaned := filepath.Clean(exportRoot)
	if cleaned != exportRoot {
		return fmt.Errorf("export root must be canonical: %s", exportRoot)
	}
	return nil
}

func nfsMountOptionsFromCapability(capability *csi.VolumeCapability) []string {
	if capability == nil || capability.GetMount() == nil {
		return arcamount.MergeNFSMountOptions(nil)
	}

	return arcamount.MergeNFSMountOptions(capability.GetMount().GetMountFlags())
}

func bindMountOptions() []string {
	return []string{"bind"}
}

func readonlyBindRemountOptions() []string {
	return []string{"bind", "remount", "ro"}
}

func validateExistingPublishReadOnly(mounter mountutils.Interface, targetPath string, readOnly bool) error {
	mountPoints, err := mounter.List()
	if err != nil {
		return fmt.Errorf("failed to list mount points: %w", err)
	}

	mountPoint, ok := findMountPoint(mountPoints, targetPath)
	if !ok {
		return fmt.Errorf("target path %s is mounted but no mount record was found", targetPath)
	}
	activeReadOnly := mountPointHasOption(mountPoint, "ro")
	if activeReadOnly != readOnly {
		return fmt.Errorf("target path %s readonly mismatch: active=%t requested=%t", targetPath, activeReadOnly, readOnly)
	}
	return nil
}

func findMountPoint(mountPoints []mountutils.MountPoint, targetPath string) (mountutils.MountPoint, bool) {
	candidates := map[string]struct{}{
		filepath.Clean(targetPath): {},
	}
	if resolved, err := filepath.EvalSymlinks(targetPath); err == nil {
		candidates[filepath.Clean(resolved)] = struct{}{}
	}

	var match mountutils.MountPoint
	found := false
	for _, mountPoint := range mountPoints {
		if _, ok := candidates[filepath.Clean(mountPoint.Path)]; ok {
			match = mountPoint
			found = true
		}
	}
	return match, found
}

func mountPointHasOption(mountPoint mountutils.MountPoint, option string) bool {
	for _, opt := range mountPoint.Opts {
		if opt == option {
			return true
		}
	}
	return false
}

func (d *Driver) mounter() mountutils.Interface {
	if d.nodeMounter != nil {
		return d.nodeMounter
	}
	return mountutils.New("")
}

func (d *Driver) sourceValidator() arcamount.MountSourceValidator {
	if d.mountSourceValidator != nil {
		return d.mountSourceValidator
	}
	return arcamount.ProcMountInfoSourceValidator{}
}

func (d *Driver) lockNodeSVM(svmName string) func() {
	if svmName == "" {
		return func() {}
	}

	d.nodeSVMLocksMu.Lock()
	if d.nodeSVMLocks == nil {
		d.nodeSVMLocks = make(map[string]*nodeSVMLock)
	}
	lock := d.nodeSVMLocks[svmName]
	if lock == nil {
		lock = &nodeSVMLock{}
		d.nodeSVMLocks[svmName] = lock
	}
	lock.refs++
	d.nodeSVMLocksMu.Unlock()

	lock.mu.Lock()

	return func() {
		lock.mu.Unlock()

		d.nodeSVMLocksMu.Lock()
		defer d.nodeSVMLocksMu.Unlock()
		lock.refs--
		if lock.refs == 0 {
			delete(d.nodeSVMLocks, svmName)
		}
	}
}

func (d *Driver) cleanupUnusedSVMMount(ctx context.Context, svmName string) {
	if svmName == "" || d.mountManager == nil {
		return
	}

	shouldUnmount, err := d.mountManager.ShouldUnmountSVM(ctx, svmName)
	if err != nil {
		klog.Warningf("Failed to check if SVM %s should be unmounted: %v", svmName, err)
		return
	}
	if !shouldUnmount {
		return
	}

	klog.V(4).Infof("Unmounting SVM %s (no more staged volumes)", svmName)
	if err := d.mountManager.UnmountSVM(ctx, svmName); err != nil {
		klog.Warningf("Failed to unmount SVM %s: %v", svmName, err)
	}
}

func (d *Driver) validateStagedMountForPublish(volumeID, stagingTargetPath string, mounter mountutils.Interface) error {
	staging, err := d.nodeState.GetVolumeStaging(volumeID)
	if err != nil {
		return status.Errorf(codes.FailedPrecondition, "staging path %s cannot be used for volume %s: %v", stagingTargetPath, volumeID, err)
	}
	if staging.StagingPath != stagingTargetPath {
		return status.Errorf(
			codes.FailedPrecondition,
			"staging path %s cannot be used for volume %s: staging path mismatch: recorded=%s requested=%s",
			stagingTargetPath,
			volumeID,
			staging.StagingPath,
			stagingTargetPath,
		)
	}

	notMnt, err := mounter.IsLikelyNotMountPoint(stagingTargetPath)
	if err != nil {
		if os.IsNotExist(err) {
			return status.Errorf(codes.FailedPrecondition, "staging path %s is not mounted", stagingTargetPath)
		}
		return status.Errorf(codes.Internal, "failed to check staging mount point: %v", err)
	}
	if notMnt {
		return status.Errorf(codes.FailedPrecondition, "staging path %s is not mounted", stagingTargetPath)
	}

	if err := validateSVMName(staging.SVMName); err != nil {
		return status.Errorf(codes.FailedPrecondition, "recorded SVM name for volume %s is invalid: %v", volumeID, err)
	}
	if err := validateVolumePath(staging.VolumePath); err != nil {
		return status.Errorf(codes.FailedPrecondition, "recorded volume path for volume %s is invalid: %v", volumeID, err)
	}

	svmMountPath, err := d.mountManager.GetMountPath(staging.SVMName)
	if err != nil {
		return status.Errorf(codes.FailedPrecondition, "SVM mount for volume %s is not available: %v", volumeID, err)
	}
	expectedSource := filepath.Join(svmMountPath, staging.VolumePath)
	if err := d.sourceValidator().ValidateMountSource(stagingTargetPath, expectedSource); err != nil {
		return status.Errorf(codes.FailedPrecondition, "staging path %s does not match recorded source: %v", stagingTargetPath, err)
	}

	return nil
}

// NodeStageVolume mounts the volume to a staging path
func (d *Driver) NodeStageVolume(ctx context.Context, req *csi.NodeStageVolumeRequest) (*csi.NodeStageVolumeResponse, error) {
	klog.V(4).Infof("NodeStageVolume called with volumeID: %s", req.GetVolumeId())

	if err := d.ensureNodeServiceConfigured(); err != nil {
		return nil, err
	}

	// Validate request
	volumeID := req.GetVolumeId()
	if volumeID == "" {
		return nil, status.Error(codes.InvalidArgument, "volume ID is required")
	}

	stagingTargetPath := req.GetStagingTargetPath()
	if stagingTargetPath == "" {
		return nil, status.Error(codes.InvalidArgument, "staging target path is required")
	}

	if req.GetVolumeCapability() == nil {
		return nil, status.Error(codes.InvalidArgument, "volume capability is required")
	}

	// Extract volume context
	volumeContext := req.GetVolumeContext()
	svmName := volumeContext[volumeContextSVM]
	vip := volumeContext[volumeContextVIP]
	exportRoot := defaultExportRoot(svmName, volumeContext[volumeContextExportRoot])
	volumePath := volumeContext[volumeContextVolumePath]

	if svmName == "" || vip == "" || volumePath == "" {
		return nil, status.Error(codes.InvalidArgument, "volume context must contain svm, vip, and volumePath")
	}

	if err := validateSVMName(svmName); err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "invalid SVM name: %v", err)
	}

	// Validate VIP to prevent injection attacks
	if err := validateVIP(vip); err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "invalid VIP: %v", err)
	}

	if err := validateExportRoot(exportRoot); err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "invalid export root: %v", err)
	}

	// Validate volume path to prevent path traversal attacks
	if err := validateVolumePath(volumePath); err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "invalid volume path: %v", err)
	}

	unlockSVM := d.lockNodeSVM(svmName)
	defer unlockSVM()

	klog.V(4).Infof("Staging volume %s (SVM: %s, VIP: %s, Path: %s) to %s", volumeID, svmName, vip, volumePath, stagingTargetPath)

	// Ensure per-SVM shared mount exists
	nfsMountOptions := nfsMountOptionsFromCapability(req.GetVolumeCapability())
	svmMountPath, err := d.mountManager.EnsureSVMMount(ctx, svmName, vip, exportRoot, nfsMountOptions)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "failed to ensure SVM mount: %v", err)
	}

	// Create staging target directory
	if err := os.MkdirAll(stagingTargetPath, 0750); err != nil {
		d.cleanupUnusedSVMMount(ctx, svmName)
		return nil, status.Errorf(codes.Internal, "failed to create staging target directory: %v", err)
	}

	// Source path is the volume subdirectory in the SVM mount
	sourcePath := filepath.Join(svmMountPath, volumePath)

	// Check if already mounted
	mounter := d.mounter()
	notMnt, err := mounter.IsLikelyNotMountPoint(stagingTargetPath)
	if err != nil {
		if !os.IsNotExist(err) {
			d.cleanupUnusedSVMMount(ctx, svmName)
			return nil, status.Errorf(codes.Internal, "failed to check mount point: %v", err)
		}
		notMnt = true
	}

	if !notMnt {
		if err := d.sourceValidator().ValidateMountSource(stagingTargetPath, sourcePath); err != nil {
			d.cleanupUnusedSVMMount(ctx, svmName)
			return nil, status.Errorf(codes.FailedPrecondition, "staging path %s is already mounted but does not match requested source: %v", stagingTargetPath, err)
		}
		if err := d.nodeState.ValidateVolumeStaging(volumeID, svmName, vip, exportRoot, volumePath, stagingTargetPath, nfsMountOptions); err != nil {
			d.cleanupUnusedSVMMount(ctx, svmName)
			return nil, status.Errorf(codes.FailedPrecondition, "staging path %s is already mounted but does not match requested volume: %v", stagingTargetPath, err)
		}
		klog.V(4).Infof("Volume %s already staged at %s", volumeID, stagingTargetPath)
		return &csi.NodeStageVolumeResponse{}, nil
	}

	// Create bind mount from SVM mount to staging path
	klog.V(4).Infof("Creating bind mount from %s to %s", sourcePath, stagingTargetPath)

	if err := mounter.Mount(sourcePath, stagingTargetPath, "", bindMountOptions()); err != nil {
		d.cleanupUnusedSVMMount(ctx, svmName)
		return nil, status.Errorf(codes.Internal, "failed to bind mount: %v", err)
	}

	// Record volume staging in NodeState
	if err := d.nodeState.RecordVolumeStaging(volumeID, svmName, vip, exportRoot, volumePath, stagingTargetPath, nfsMountOptions); err != nil {
		klog.Warningf("Failed to record volume staging in node state, rolling back mount: %v", err)

		// Best-effort: revert in-memory state (may also fail to persist)
		if rmErr := d.nodeState.RemoveVolumeStaging(volumeID); rmErr != nil {
			klog.Warningf("Failed to remove volume staging from node state during rollback: %v", rmErr)
		}

		// Best-effort: unmount and remove staging directory
		if umErr := mounter.Unmount(stagingTargetPath); umErr != nil {
			klog.Warningf("Failed to unmount staging target path %s during rollback: %v", stagingTargetPath, umErr)
		}
		if rmDirErr := os.Remove(stagingTargetPath); rmDirErr != nil && !os.IsNotExist(rmDirErr) {
			klog.Warningf("Failed to remove staging target directory %s during rollback: %v", stagingTargetPath, rmDirErr)
		}

		d.cleanupUnusedSVMMount(ctx, svmName)
		return nil, status.Errorf(codes.Internal, "failed to persist node state for volume staging: %v", err)
	}

	klog.Infof("Volume %s staged successfully at %s", volumeID, stagingTargetPath)

	return &csi.NodeStageVolumeResponse{}, nil
}

// NodeUnstageVolume unmounts the volume from the staging path
func (d *Driver) NodeUnstageVolume(ctx context.Context, req *csi.NodeUnstageVolumeRequest) (*csi.NodeUnstageVolumeResponse, error) {
	klog.V(4).Infof("NodeUnstageVolume called with volumeID: %s", req.GetVolumeId())

	if err := d.ensureNodeServiceConfigured(); err != nil {
		return nil, err
	}

	volumeID := req.GetVolumeId()
	if volumeID == "" {
		return nil, status.Error(codes.InvalidArgument, "volume ID is required")
	}

	stagingTargetPath := req.GetStagingTargetPath()
	if stagingTargetPath == "" {
		return nil, status.Error(codes.InvalidArgument, "staging target path is required")
	}

	klog.V(4).Infof("Unstaging volume %s from %s", volumeID, stagingTargetPath)

	// Get SVM name from NodeState
	svmName, err := d.nodeState.GetSVMForVolumeFresh(volumeID)
	if err != nil {
		klog.Warningf("Volume %s not found in node state: %v", volumeID, err)
		// Continue with unmount attempt
		svmName = ""
	}
	unlockSVM := d.lockNodeSVM(svmName)
	defer unlockSVM()

	// Unmount the staging path
	mounter := d.mounter()
	notMnt, err := mounter.IsLikelyNotMountPoint(stagingTargetPath)
	if err != nil {
		if os.IsNotExist(err) {
			klog.V(4).Infof("Staging path %s does not exist, considering volume unstaged", stagingTargetPath)
			// Clean up NodeState
			if err := d.nodeState.RemoveVolumeStaging(volumeID); err != nil {
				klog.Warningf("Failed to remove volume staging from node state: %v", err)
			}
			if svmName != "" {
				d.cleanupUnusedSVMMount(ctx, svmName)
			}
			return &csi.NodeUnstageVolumeResponse{}, nil
		}
		return nil, status.Errorf(codes.Internal, "failed to check mount point: %v", err)
	}

	if !notMnt {
		klog.V(4).Infof("Unmounting %s", stagingTargetPath)
		if err := mounter.Unmount(stagingTargetPath); err != nil {
			return nil, status.Errorf(codes.Internal, "failed to unmount: %v", err)
		}
	}

	// Remove staging directory
	if err := os.Remove(stagingTargetPath); err != nil && !os.IsNotExist(err) {
		klog.Warningf("Failed to remove staging directory %s: %v", stagingTargetPath, err)
	}

	// Remove from NodeState
	if err := d.nodeState.RemoveVolumeStaging(volumeID); err != nil {
		klog.Warningf("Failed to remove volume staging from node state: %v", err)
	}

	// Check if SVM mount should be unmounted (derived refcount check)
	if svmName != "" {
		d.cleanupUnusedSVMMount(ctx, svmName)
	}

	klog.Infof("Volume %s unstaged successfully from %s", volumeID, stagingTargetPath)

	return &csi.NodeUnstageVolumeResponse{}, nil
}

// NodePublishVolume mounts the volume to the target path
func (d *Driver) NodePublishVolume(ctx context.Context, req *csi.NodePublishVolumeRequest) (*csi.NodePublishVolumeResponse, error) {
	klog.V(4).Infof("NodePublishVolume called with volumeID: %s", req.GetVolumeId())

	if err := d.ensureNodeServiceConfigured(); err != nil {
		return nil, err
	}

	// Validate request
	volumeID := req.GetVolumeId()
	if volumeID == "" {
		return nil, status.Error(codes.InvalidArgument, "volume ID is required")
	}

	stagingTargetPath := req.GetStagingTargetPath()
	if stagingTargetPath == "" {
		return nil, status.Error(codes.InvalidArgument, "staging target path is required")
	}

	targetPath := req.GetTargetPath()
	if targetPath == "" {
		return nil, status.Error(codes.InvalidArgument, "target path is required")
	}

	if req.GetVolumeCapability() == nil {
		return nil, status.Error(codes.InvalidArgument, "volume capability is required")
	}

	klog.V(4).Infof("Publishing volume %s from %s to %s", volumeID, stagingTargetPath, targetPath)
	readonly := req.GetReadonly()

	// Check if already mounted
	mounter := d.mounter()
	notMnt, err := mounter.IsLikelyNotMountPoint(targetPath)
	if err != nil {
		if !os.IsNotExist(err) {
			return nil, status.Errorf(codes.Internal, "failed to check mount point: %v", err)
		}
		notMnt = true
	}

	if !notMnt {
		if err := d.sourceValidator().ValidateMountSource(targetPath, stagingTargetPath); err != nil {
			return nil, status.Errorf(codes.FailedPrecondition, "target path %s is already mounted but does not match requested source: %v", targetPath, err)
		}
		if err := d.nodeState.ValidateVolumePublish(volumeID, targetPath, readonly); err != nil {
			return nil, status.Errorf(codes.FailedPrecondition, "target path %s is already mounted but cannot be reused: %v", targetPath, err)
		}
		if err := d.validateStagedMountForPublish(volumeID, stagingTargetPath, mounter); err != nil {
			return nil, err
		}
		if err := validateExistingPublishReadOnly(mounter, targetPath, readonly); err != nil {
			return nil, status.Errorf(codes.FailedPrecondition, "target path %s is already mounted but cannot be reused: %v", targetPath, err)
		}
		klog.V(4).Infof("Volume %s already published at %s", volumeID, targetPath)
		return &csi.NodePublishVolumeResponse{}, nil
	}

	if err := d.validateStagedMountForPublish(volumeID, stagingTargetPath, mounter); err != nil {
		return nil, err
	}

	// Create target directory
	if err := os.MkdirAll(targetPath, 0750); err != nil {
		return nil, status.Errorf(codes.Internal, "failed to create target directory: %v", err)
	}

	// Step 1: Create initial bind mount
	mountOptions := bindMountOptions()
	klog.V(4).Infof("Creating bind mount from %s to %s with options: %v", stagingTargetPath, targetPath, mountOptions)
	if err := mounter.Mount(stagingTargetPath, targetPath, "", mountOptions); err != nil {
		return nil, status.Errorf(codes.Internal, "failed to bind mount: %v", err)
	}

	// Step 2: If read-only is requested, remount with 'ro' flag to enforce it
	// (Linux requires separate remount to properly enforce read-only on bind mounts)
	if readonly {
		klog.V(4).Infof("Remounting %s as read-only", targetPath)
		if err := mounter.Mount(stagingTargetPath, targetPath, "", readonlyBindRemountOptions()); err != nil {
			// Rollback: unmount the initial bind mount
			klog.Errorf("Failed to remount as read-only, rolling back: %v", err)
			if unmountErr := mounter.Unmount(targetPath); unmountErr != nil {
				klog.Errorf("Failed to rollback bind mount: %v", unmountErr)
			}
			if rmErr := os.Remove(targetPath); rmErr != nil && !os.IsNotExist(rmErr) {
				klog.Warningf("Failed to remove target path %s during rollback: %v", targetPath, rmErr)
			}
			return nil, status.Errorf(codes.Internal, "failed to remount as read-only: %v", err)
		}
	}

	// Record volume publish in NodeState
	if err := d.nodeState.RecordVolumePublish(volumeID, targetPath, readonly); err != nil {
		klog.Warningf("Failed to record volume publish in node state, rolling back mount: %v", err)

		// Best-effort: revert in-memory state (may also fail to persist)
		if rmErr := d.nodeState.RemoveVolumePublish(volumeID, targetPath); rmErr != nil {
			klog.Warningf("Failed to remove volume publish from node state during rollback: %v", rmErr)
		}

		// Best-effort: unmount and remove target directory
		if umErr := mounter.Unmount(targetPath); umErr != nil {
			klog.Warningf("Failed to unmount target path %s during rollback: %v", targetPath, umErr)
		}
		if rmDirErr := os.Remove(targetPath); rmDirErr != nil && !os.IsNotExist(rmDirErr) {
			klog.Warningf("Failed to remove target directory %s during rollback: %v", targetPath, rmDirErr)
		}

		return nil, status.Errorf(codes.Internal, "failed to persist node state for volume publish: %v", err)
	}

	klog.Infof("Volume %s published successfully at %s", volumeID, targetPath)

	return &csi.NodePublishVolumeResponse{}, nil
}

// NodeUnpublishVolume unmounts the volume from the target path
func (d *Driver) NodeUnpublishVolume(ctx context.Context, req *csi.NodeUnpublishVolumeRequest) (*csi.NodeUnpublishVolumeResponse, error) {
	klog.V(4).Infof("NodeUnpublishVolume called with volumeID: %s", req.GetVolumeId())

	if err := d.ensureNodeServiceConfigured(); err != nil {
		return nil, err
	}

	volumeID := req.GetVolumeId()
	if volumeID == "" {
		return nil, status.Error(codes.InvalidArgument, "volume ID is required")
	}

	targetPath := req.GetTargetPath()
	if targetPath == "" {
		return nil, status.Error(codes.InvalidArgument, "target path is required")
	}

	klog.V(4).Infof("Unpublishing volume %s from %s", volumeID, targetPath)

	// Unmount the target path
	mounter := d.mounter()
	notMnt, err := mounter.IsLikelyNotMountPoint(targetPath)
	if err != nil {
		if os.IsNotExist(err) {
			klog.V(4).Infof("Target path %s does not exist, considering volume unpublished", targetPath)
			// Clean up NodeState
			if err := d.nodeState.RemoveVolumePublish(volumeID, targetPath); err != nil {
				klog.Warningf("Failed to remove volume publish from node state: %v", err)
			}
			return &csi.NodeUnpublishVolumeResponse{}, nil
		}
		return nil, status.Errorf(codes.Internal, "failed to check mount point: %v", err)
	}

	if !notMnt {
		klog.V(4).Infof("Unmounting %s", targetPath)
		if err := mounter.Unmount(targetPath); err != nil {
			return nil, status.Errorf(codes.Internal, "failed to unmount: %v", err)
		}
	}

	// Remove target directory
	if err := os.Remove(targetPath); err != nil && !os.IsNotExist(err) {
		klog.Warningf("Failed to remove target directory %s: %v", targetPath, err)
	}

	// Remove from NodeState
	if err := d.nodeState.RemoveVolumePublish(volumeID, targetPath); err != nil {
		klog.Warningf("Failed to remove volume publish from node state: %v", err)
	}

	klog.Infof("Volume %s unpublished successfully from %s", volumeID, targetPath)

	return &csi.NodeUnpublishVolumeResponse{}, nil
}

// NodeGetVolumeStats returns volume usage statistics
func (d *Driver) NodeGetVolumeStats(ctx context.Context, req *csi.NodeGetVolumeStatsRequest) (*csi.NodeGetVolumeStatsResponse, error) {
	klog.V(4).Infof("NodeGetVolumeStats called with volumeID: %s", req.GetVolumeId())

	if err := d.ensureNodeServiceConfigured(); err != nil {
		return nil, err
	}

	volumeID := req.GetVolumeId()
	if volumeID == "" {
		return nil, status.Error(codes.InvalidArgument, "volume ID is required")
	}

	volumePath := req.GetVolumePath()
	if volumePath == "" {
		return nil, status.Error(codes.InvalidArgument, "volume path is required")
	}

	// Check if path exists
	if _, err := os.Stat(volumePath); err != nil {
		if os.IsNotExist(err) {
			return nil, status.Errorf(codes.NotFound, "volume path %s does not exist", volumePath)
		}
		return nil, status.Errorf(codes.Internal, "failed to stat volume path: %v", err)
	}

	var fs syscall.Statfs_t
	if err := syscall.Statfs(volumePath, &fs); err != nil {
		return nil, status.Errorf(codes.Internal, "failed to statfs volume path: %v", err)
	}

	blockSize := int64(fs.Bsize)
	usedBlocks := uint64(0)
	if fs.Blocks > fs.Bfree {
		usedBlocks = fs.Blocks - fs.Bfree
	}
	totalBytes := int64(fs.Blocks) * blockSize
	availableBytes := int64(fs.Bavail) * blockSize
	usedBytes := int64(usedBlocks) * blockSize
	totalInodes := int64(fs.Files)
	availableInodes := int64(fs.Ffree)
	usedInodes := totalInodes - availableInodes
	if usedInodes < 0 {
		usedInodes = 0
	}

	return &csi.NodeGetVolumeStatsResponse{
		Usage: []*csi.VolumeUsage{
			{
				Available: availableBytes,
				Total:     totalBytes,
				Used:      usedBytes,
				Unit:      csi.VolumeUsage_BYTES,
			},
			{
				Available: availableInodes,
				Total:     totalInodes,
				Used:      usedInodes,
				Unit:      csi.VolumeUsage_INODES,
			},
		},
	}, nil
}

// NodeExpandVolume expands the volume (no-op for NFS)
func (d *Driver) NodeExpandVolume(ctx context.Context, req *csi.NodeExpandVolumeRequest) (*csi.NodeExpandVolumeResponse, error) {
	klog.V(4).Infof("NodeExpandVolume called with volumeID: %s", req.GetVolumeId())

	if err := d.ensureNodeServiceConfigured(); err != nil {
		return nil, err
	}

	volumeID := req.GetVolumeId()
	if volumeID == "" {
		return nil, status.Error(codes.InvalidArgument, "volume ID is required")
	}

	// NFS volumes don't require node-side expansion
	// The quota expansion is handled by the controller
	klog.V(4).Infof("Volume %s expansion is handled server-side, no node action required", volumeID)

	return &csi.NodeExpandVolumeResponse{}, nil
}

// NodeGetCapabilities returns node capabilities
func (d *Driver) NodeGetCapabilities(ctx context.Context, req *csi.NodeGetCapabilitiesRequest) (*csi.NodeGetCapabilitiesResponse, error) {
	klog.V(4).Infof("NodeGetCapabilities called")

	if err := d.ensureNodeServiceConfigured(); err != nil {
		return nil, err
	}

	capabilities := []csi.NodeServiceCapability_RPC_Type{
		csi.NodeServiceCapability_RPC_STAGE_UNSTAGE_VOLUME,
		csi.NodeServiceCapability_RPC_GET_VOLUME_STATS,
		csi.NodeServiceCapability_RPC_EXPAND_VOLUME,
	}

	caps := make([]*csi.NodeServiceCapability, len(capabilities))
	for i, cap := range capabilities {
		caps[i] = &csi.NodeServiceCapability{
			Type: &csi.NodeServiceCapability_Rpc{
				Rpc: &csi.NodeServiceCapability_RPC{
					Type: cap,
				},
			},
		}
	}

	return &csi.NodeGetCapabilitiesResponse{
		Capabilities: caps,
	}, nil
}

// NodeGetInfo returns node information
func (d *Driver) NodeGetInfo(ctx context.Context, req *csi.NodeGetInfoRequest) (*csi.NodeGetInfoResponse, error) {
	klog.V(4).Infof("NodeGetInfo called")

	if d.nodeID == "" {
		return nil, status.Error(codes.Unavailable, "node ID not configured")
	}

	return &csi.NodeGetInfoResponse{
		NodeId: d.nodeID,
	}, nil
}
