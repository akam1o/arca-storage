package driver

import (
	"context"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"

	"github.com/container-storage-interface/spec/lib/go/csi"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"k8s.io/klog/v2"
	"k8s.io/mount-utils"
)

func (d *Driver) ensureNodeServiceConfigured() error {
	if d.mode != "node" {
		return status.Errorf(codes.FailedPrecondition, "node service is not available in %s mode", d.mode)
	}
	if d.nodeID == "" || d.nodeState == nil || d.mountManager == nil {
		return status.Error(codes.FailedPrecondition, "node service is not configured (run as node plugin with node-id)")
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
	if strings.Contains(cleaned, "..") {
		return fmt.Errorf("volume path contains invalid traversal pattern: %s", path)
	}

	// Ensure cleaned path doesn't escape (starts with ..)
	if strings.HasPrefix(cleaned, "..") {
		return fmt.Errorf("volume path attempts to escape root: %s", path)
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
	volumePath := volumeContext[volumeContextVolumePath]

	if svmName == "" || vip == "" || volumePath == "" {
		return nil, status.Error(codes.InvalidArgument, "volume context must contain svm, vip, and volumePath")
	}

	// Validate VIP to prevent injection attacks
	if err := validateVIP(vip); err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "invalid VIP: %v", err)
	}

	// Validate volume path to prevent path traversal attacks
	if err := validateVolumePath(volumePath); err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "invalid volume path: %v", err)
	}

	klog.V(4).Infof("Staging volume %s (SVM: %s, VIP: %s, Path: %s) to %s", volumeID, svmName, vip, volumePath, stagingTargetPath)

	// Ensure per-SVM shared mount exists
	svmMountPath, err := d.mountManager.EnsureSVMMount(ctx, svmName, vip)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "failed to ensure SVM mount: %v", err)
	}

	// Create staging target directory
	if err := os.MkdirAll(stagingTargetPath, 0750); err != nil {
		return nil, status.Errorf(codes.Internal, "failed to create staging target directory: %v", err)
	}

	// Source path is the volume subdirectory in the SVM mount
	sourcePath := filepath.Join(svmMountPath, volumePath)

	// Check if already mounted
	mounter := mount.New("")
	notMnt, err := mounter.IsLikelyNotMountPoint(stagingTargetPath)
	if err != nil {
		if !os.IsNotExist(err) {
			return nil, status.Errorf(codes.Internal, "failed to check mount point: %v", err)
		}
		notMnt = true
	}

	if !notMnt {
		klog.V(4).Infof("Volume %s already staged at %s", volumeID, stagingTargetPath)
		return &csi.NodeStageVolumeResponse{}, nil
	}

	// Create bind mount from SVM mount to staging path
	klog.V(4).Infof("Creating bind mount from %s to %s", sourcePath, stagingTargetPath)

	mountOptions := []string{"bind"}
	if err := mounter.Mount(sourcePath, stagingTargetPath, "", mountOptions); err != nil {
		return nil, status.Errorf(codes.Internal, "failed to bind mount: %v", err)
	}

	// Record volume staging in NodeState
	if err := d.nodeState.RecordVolumeStaging(volumeID, svmName, vip, stagingTargetPath); err != nil {
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
	svmName, err := d.nodeState.GetSVMForVolume(volumeID)
	if err != nil {
		klog.Warningf("Volume %s not found in node state: %v", volumeID, err)
		// Continue with unmount attempt
		svmName = ""
	}

	// Unmount the staging path
	mounter := mount.New("")
	notMnt, err := mounter.IsLikelyNotMountPoint(stagingTargetPath)
	if err != nil {
		if os.IsNotExist(err) {
			klog.V(4).Infof("Staging path %s does not exist, considering volume unstaged", stagingTargetPath)
			// Clean up NodeState
			if err := d.nodeState.RemoveVolumeStaging(volumeID); err != nil {
				klog.Warningf("Failed to remove volume staging from node state: %v", err)
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
		shouldUnmount, err := d.mountManager.ShouldUnmountSVM(ctx, svmName)
		if err != nil {
			klog.Warningf("Failed to check if SVM %s should be unmounted: %v", svmName, err)
		} else if shouldUnmount {
			klog.V(4).Infof("Unmounting SVM %s (no more staged volumes)", svmName)
			if err := d.mountManager.UnmountSVM(ctx, svmName); err != nil {
				klog.Warningf("Failed to unmount SVM %s: %v", svmName, err)
			}
		}
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

	// Create target directory
	if err := os.MkdirAll(targetPath, 0750); err != nil {
		return nil, status.Errorf(codes.Internal, "failed to create target directory: %v", err)
	}

	// Check if already mounted
	mounter := mount.New("")
	notMnt, err := mounter.IsLikelyNotMountPoint(targetPath)
	if err != nil {
		if !os.IsNotExist(err) {
			return nil, status.Errorf(codes.Internal, "failed to check mount point: %v", err)
		}
		notMnt = true
	}

	if !notMnt {
		klog.V(4).Infof("Volume %s already published at %s", volumeID, targetPath)
		return &csi.NodePublishVolumeResponse{}, nil
	}

	// Determine if read-only mount is requested
	readonly := req.GetReadonly()

	// Prepare mount options (exclude 'ro' for initial bind mount)
	mountOptions := []string{"bind"}

	// Get additional mount options from capability
	capability := req.GetVolumeCapability()
	if mountCap := capability.GetMount(); mountCap != nil {
		for _, opt := range mountCap.GetMountFlags() {
			// Skip 'ro' flag - will be applied in remount if needed
			if opt != "ro" && opt != "rw" {
				mountOptions = append(mountOptions, opt)
			}
		}
	}

	// Step 1: Create initial bind mount
	klog.V(4).Infof("Creating bind mount from %s to %s with options: %v", stagingTargetPath, targetPath, mountOptions)
	if err := mounter.Mount(stagingTargetPath, targetPath, "", mountOptions); err != nil {
		return nil, status.Errorf(codes.Internal, "failed to bind mount: %v", err)
	}

	// Step 2: If read-only is requested, remount with 'ro' flag to enforce it
	// (Linux requires separate remount to properly enforce read-only on bind mounts)
	if readonly {
		klog.V(4).Infof("Remounting %s as read-only", targetPath)
		remountOptions := append(mountOptions, "ro", "remount")
		if err := mounter.Mount(stagingTargetPath, targetPath, "", remountOptions); err != nil {
			// Rollback: unmount the initial bind mount
			klog.Errorf("Failed to remount as read-only, rolling back: %v", err)
			if unmountErr := mounter.Unmount(targetPath); unmountErr != nil {
				klog.Errorf("Failed to rollback bind mount: %v", unmountErr)
			}
			os.Remove(targetPath)
			return nil, status.Errorf(codes.Internal, "failed to remount as read-only: %v", err)
		}
	}

	// Record volume publish in NodeState
	if err := d.nodeState.RecordVolumePublish(volumeID, targetPath); err != nil {
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
	mounter := mount.New("")
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

	// For now, return minimal stats
	// In production, implement proper filesystem stats using statfs syscall
	return &csi.NodeGetVolumeStatsResponse{
		Usage: []*csi.VolumeUsage{
			{
				Unit: csi.VolumeUsage_BYTES,
			},
			{
				Unit: csi.VolumeUsage_INODES,
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
