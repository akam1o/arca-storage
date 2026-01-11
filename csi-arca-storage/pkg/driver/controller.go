package driver

import (
	"context"
	"fmt"
	"time"

	"github.com/container-storage-interface/spec/lib/go/csi"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"k8s.io/klog/v2"

	"github.com/akam1o/csi-arca-storage/pkg/arca"
	"github.com/akam1o/csi-arca-storage/pkg/store"
)

const (
	// Parameter keys
	paramNamespace = "csi.storage.k8s.io/pvc/namespace"
	paramPVCName   = "csi.storage.k8s.io/pvc/name"

	// Volume context keys
	volumeContextSVM        = "svm"
	volumeContextVIP        = "vip"
	volumeContextVolumePath = "volumePath"

	// Default capacity if not specified
	defaultCapacityBytes = 1 * 1024 * 1024 * 1024 // 1 GiB
)

// compareVolumeParameters checks if requested matches existing
func compareVolumeParameters(existing *store.VolumeInfo, req *csi.CreateVolumeRequest) error {
	// Compare capacity
	requestedBytes := int64(defaultCapacityBytes)
	if req.GetCapacityRange() != nil && req.GetCapacityRange().GetRequiredBytes() > 0 {
		requestedBytes = req.GetCapacityRange().GetRequiredBytes()
	}
	if requestedBytes != existing.CapacityBytes {
		return fmt.Errorf("capacity mismatch: requested %d, existing %d",
			requestedBytes, existing.CapacityBytes)
	}

	// Compare content source
	if !contentSourcesMatch(req.GetVolumeContentSource(), existing.ContentSource) {
		return fmt.Errorf("content source mismatch")
	}
	return nil
}

// contentSourcesMatch compares two content sources
func contentSourcesMatch(a, b *csi.VolumeContentSource) bool {
	if a == nil && b == nil {
		return true
	}
	if a == nil || b == nil {
		return false
	}

	if volA := a.GetVolume(); volA != nil {
		volB := b.GetVolume()
		return volB != nil && volA.GetVolumeId() == volB.GetVolumeId()
	}

	if snapA := a.GetSnapshot(); snapA != nil {
		snapB := b.GetSnapshot()
		return snapB != nil && snapA.GetSnapshotId() == snapB.GetSnapshotId()
	}
	return false
}

// ensureControllerServiceConfigured checks if the driver is running in controller mode
func (d *Driver) ensureControllerServiceConfigured() error {
	if d.mode != "controller" {
		return status.Errorf(codes.FailedPrecondition,
			"controller service is not available in %s mode", d.mode)
	}
	return nil
}

// CreateVolume creates a new volume
func (d *Driver) CreateVolume(ctx context.Context, req *csi.CreateVolumeRequest) (*csi.CreateVolumeResponse, error) {
	klog.V(4).Infof("CreateVolume called with name: %s", req.GetName())

	// Defensive check for correct mode
	if err := d.ensureControllerServiceConfigured(); err != nil {
		return nil, err
	}

	// Validate request
	if req.GetName() == "" {
		return nil, status.Error(codes.InvalidArgument, "volume name is required")
	}

	if req.GetVolumeCapabilities() == nil || len(req.GetVolumeCapabilities()) == 0 {
		return nil, status.Error(codes.InvalidArgument, "volume capabilities are required")
	}

	// Validate capabilities
	if err := d.validateVolumeCapabilities(req.GetVolumeCapabilities()); err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "invalid volume capabilities: %v", err)
	}

	// Extract parameters
	params := req.GetParameters()
	namespace := params[paramNamespace]
	if namespace == "" {
		return nil, status.Error(codes.InvalidArgument, "namespace parameter is required")
	}

	pvcName := params[paramPVCName]
	if pvcName == "" {
		pvcName = req.GetName()
	}

	// Generate stable volume ID (idempotent)
	volumeID := d.volumeIDGen.GenerateVolumeID(req.GetName())

	// Check if volume already exists (idempotency)
	existingVol, err := d.store.GetVolume(volumeID)
	if err == nil {
		if err := compareVolumeParameters(existingVol, req); err != nil {
			return nil, status.Errorf(codes.AlreadyExists, "volume %s already exists but is incompatible: %v", volumeID, err)
		}
		klog.V(4).Infof("Volume %s already exists, returning existing volume", volumeID)
		return &csi.CreateVolumeResponse{
			Volume: existingVol.ToCSIVolume(),
		}, nil
	}
	if !store.IsNotFound(err) {
		return nil, status.Errorf(codes.Internal, "failed to check existing volume %s: %v", volumeID, err)
	}

	// Determine capacity
	capacityBytes := int64(defaultCapacityBytes)
	if req.GetCapacityRange() != nil && req.GetCapacityRange().GetRequiredBytes() > 0 {
		capacityBytes = req.GetCapacityRange().GetRequiredBytes()
	}

	// Handle content source first to determine which SVM to use
	var svm *arca.SVM
	var contentSource *csi.VolumeContentSource

	// Determine directory path (relative path, no leading slash)
	// This will be joined with SVM mount path on the node side
	volumePath := volumeID

	if req.GetVolumeContentSource() != nil {
		src := req.GetVolumeContentSource()
		if src.GetVolume() == nil && src.GetSnapshot() == nil {
			return nil, status.Error(codes.InvalidArgument, "volume content source must set either volume or snapshot")
		}

		if src.GetVolume() != nil {
			// Clone from existing volume
			sourceVolumeID := src.GetVolume().GetVolumeId()
			klog.V(4).Infof("Cloning from source volume: %s", sourceVolumeID)

			sourceVol, err := d.store.GetVolume(sourceVolumeID)
			if err != nil {
				return nil, status.Errorf(codes.NotFound, "source volume %s not found: %v", sourceVolumeID, err)
			}

			// Clone must use the same SVM as the source volume
			svm = &arca.SVM{
				Name: sourceVol.SVMName,
				VIP:  sourceVol.VIP,
			}
			klog.V(4).Infof("Using source SVM for clone: %s with VIP: %s", svm.Name, svm.VIP)

			// Create snapshot of source volume first (server-side reflink)
			err = d.arcaClient.CreateSnapshot(ctx, &arca.CreateSnapshotRequest{
				SVMName:      sourceVol.SVMName,
				SourcePath:   sourceVol.Path,
				SnapshotPath: volumePath,
			})
			if err != nil && !arca.IsAlreadyExistsError(err) {
				return nil, status.Errorf(codes.Internal, "failed to clone volume: %v", err)
			}

			contentSource = &csi.VolumeContentSource{
				Type: &csi.VolumeContentSource_Volume{
					Volume: &csi.VolumeContentSource_VolumeSource{
						VolumeId: sourceVolumeID,
					},
				},
			}

			klog.V(4).Infof("Volume cloned successfully from %s", sourceVolumeID)

		} else if src.GetSnapshot() != nil {
			// Restore from snapshot
			snapshotID := src.GetSnapshot().GetSnapshotId()
			klog.V(4).Infof("Restoring from snapshot: %s", snapshotID)

			snapshot, err := d.store.GetSnapshot(snapshotID)
			if err != nil {
				return nil, status.Errorf(codes.NotFound, "snapshot %s not found: %v", snapshotID, err)
			}

			if !snapshot.ReadyToUse {
				return nil, status.Errorf(codes.Unavailable, "snapshot %s is not ready", snapshotID)
			}

			// Restore must use the same SVM as the snapshot
			svm, err = d.arcaClient.GetSVM(ctx, snapshot.SVMName)
			if err != nil {
				return nil, status.Errorf(codes.Internal, "failed to get SVM %s for snapshot restore: %v", snapshot.SVMName, err)
			}
			klog.V(4).Infof("Using snapshot SVM for restore: %s (VIP: %s)", svm.Name, svm.VIP)

			// Copy snapshot to new volume path (server-side reflink)
			err = d.arcaClient.CreateSnapshot(ctx, &arca.CreateSnapshotRequest{
				SVMName:      snapshot.SVMName,
				SourcePath:   snapshot.Path,
				SnapshotPath: volumePath,
			})
			if err != nil && !arca.IsAlreadyExistsError(err) {
				return nil, status.Errorf(codes.Internal, "failed to restore from snapshot: %v", err)
			}

			contentSource = &csi.VolumeContentSource{
				Type: &csi.VolumeContentSource_Snapshot{
					Snapshot: &csi.VolumeContentSource_SnapshotSource{
						SnapshotId: snapshotID,
					},
				},
			}

			klog.V(4).Infof("Volume restored successfully from snapshot %s", snapshotID)
		}
	} else {
		// No content source - create new volume
		// Ensure SVM exists for this namespace
		klog.V(4).Infof("Ensuring SVM exists for namespace: %s", namespace)
		var err error
		svm, err = d.svmManager.EnsureSVM(ctx, namespace)
		if err != nil {
			return nil, status.Errorf(codes.Internal, "failed to ensure SVM: %v", err)
		}
		klog.V(4).Infof("Using SVM: %s with VIP: %s", svm.Name, svm.VIP)

		// Create new directory
		klog.V(4).Infof("Creating new directory: %s", volumePath)
		err = d.arcaClient.CreateDirectory(ctx, &arca.CreateDirectoryRequest{
			SVMName: svm.Name,
			Path:    volumePath,
		})
		if err != nil && !arca.IsAlreadyExistsError(err) {
			return nil, status.Errorf(codes.Internal, "failed to create directory: %v", err)
		}
	}

	// Set quota
	klog.V(4).Infof("Setting quota for volume %s: %d bytes", volumeID, capacityBytes)
	err = d.arcaClient.SetQuota(ctx, &arca.SetQuotaRequest{
		SVMName:    svm.Name,
		Path:       volumePath,
		QuotaBytes: capacityBytes,
	})
	if err != nil {
		return nil, status.Errorf(codes.Internal, "failed to set quota: %v", err)
	}

	// Store volume metadata
	volumeInfo := &store.VolumeInfo{
		VolumeID:      volumeID,
		Name:          pvcName,
		SVMName:       svm.Name,
		VIP:           svm.VIP,
		Path:          volumePath,
		CapacityBytes: capacityBytes,
		CreatedAt:     time.Now(),
		ContentSource: contentSource,
	}

	if err := d.store.CreateVolume(volumeInfo); err != nil {
		if store.IsAlreadyExists(err) {
			existingVol, getErr := d.store.GetVolume(volumeID)
			if getErr == nil {
				if err := compareVolumeParameters(existingVol, req); err != nil {
					return nil, status.Errorf(codes.AlreadyExists, "volume %s already exists but is incompatible: %v", volumeID, err)
				}
				return &csi.CreateVolumeResponse{Volume: existingVol.ToCSIVolume()}, nil
			}
		}
		return nil, status.Errorf(codes.Internal, "failed to store volume metadata: %v", err)
	}

	klog.Infof("Volume %s created successfully (SVM: %s, Path: %s)", volumeID, svm.Name, volumePath)

	return &csi.CreateVolumeResponse{
		Volume: volumeInfo.ToCSIVolume(),
	}, nil
}

// DeleteVolume deletes a volume
func (d *Driver) DeleteVolume(ctx context.Context, req *csi.DeleteVolumeRequest) (*csi.DeleteVolumeResponse, error) {
	klog.V(4).Infof("DeleteVolume called with volumeID: %s", req.GetVolumeId())

	if err := d.ensureControllerServiceConfigured(); err != nil {
		return nil, err
	}

	volumeID := req.GetVolumeId()
	if volumeID == "" {
		return nil, status.Error(codes.InvalidArgument, "volume ID is required")
	}

	// Get volume info
	volumeInfo, err := d.store.GetVolume(volumeID)
	if err != nil {
		if store.IsNotFound(err) {
			// Volume doesn't exist in our store - idempotent success
			klog.V(4).Infof("Volume %s not found in store, considering it already deleted", volumeID)
			return &csi.DeleteVolumeResponse{}, nil
		}
		return nil, status.Errorf(codes.Internal, "failed to get volume %s: %v", volumeID, err)
	}

	// Delete directory from ARCA
	klog.V(4).Infof("Deleting directory: %s on SVM: %s", volumeInfo.Path, volumeInfo.SVMName)
	err = d.arcaClient.DeleteDirectory(ctx, volumeInfo.SVMName, volumeInfo.Path)
	if err != nil && !arca.IsNotFoundError(err) {
		return nil, status.Errorf(codes.Internal, "failed to delete directory: %v", err)
	}

	// Delete volume metadata - MUST succeed for proper cleanup
	if err := d.store.DeleteVolume(volumeID); err != nil {
		// Only ignore if already deleted (idempotent)
		if !store.IsNotFound(err) {
			return nil, status.Errorf(codes.Internal, "failed to delete volume metadata: %v", err)
		}
		klog.V(4).Infof("Volume metadata %s already deleted", volumeID)
	}

	klog.Infof("Volume %s deleted successfully", volumeID)

	return &csi.DeleteVolumeResponse{}, nil
}

// ControllerPublishVolume is not supported for NFS
func (d *Driver) ControllerPublishVolume(ctx context.Context, req *csi.ControllerPublishVolumeRequest) (*csi.ControllerPublishVolumeResponse, error) {
	return nil, status.Error(codes.Unimplemented, "ControllerPublishVolume is not supported for NFS")
}

// ControllerUnpublishVolume is not supported for NFS
func (d *Driver) ControllerUnpublishVolume(ctx context.Context, req *csi.ControllerUnpublishVolumeRequest) (*csi.ControllerUnpublishVolumeResponse, error) {
	return nil, status.Error(codes.Unimplemented, "ControllerUnpublishVolume is not supported for NFS")
}

// ValidateVolumeCapabilities validates volume capabilities
func (d *Driver) ValidateVolumeCapabilities(ctx context.Context, req *csi.ValidateVolumeCapabilitiesRequest) (*csi.ValidateVolumeCapabilitiesResponse, error) {
	klog.V(4).Infof("ValidateVolumeCapabilities called with volumeID: %s", req.GetVolumeId())

	if err := d.ensureControllerServiceConfigured(); err != nil {
		return nil, err
	}

	volumeID := req.GetVolumeId()
	if volumeID == "" {
		return nil, status.Error(codes.InvalidArgument, "volume ID is required")
	}

	if req.GetVolumeCapabilities() == nil || len(req.GetVolumeCapabilities()) == 0 {
		return nil, status.Error(codes.InvalidArgument, "volume capabilities are required")
	}

	// Check if volume exists
	_, err := d.store.GetVolume(volumeID)
	if err != nil {
		return nil, status.Errorf(codes.NotFound, "volume %s not found", volumeID)
	}

	// Validate capabilities
	if err := d.validateVolumeCapabilities(req.GetVolumeCapabilities()); err != nil {
		return &csi.ValidateVolumeCapabilitiesResponse{
			Message: err.Error(),
		}, nil
	}

	return &csi.ValidateVolumeCapabilitiesResponse{
		Confirmed: &csi.ValidateVolumeCapabilitiesResponse_Confirmed{
			VolumeCapabilities: req.GetVolumeCapabilities(),
		},
	}, nil
}

// ListVolumes lists volumes with pagination
func (d *Driver) ListVolumes(ctx context.Context, req *csi.ListVolumesRequest) (*csi.ListVolumesResponse, error) {
	klog.V(4).Infof("ListVolumes called")

	if err := d.ensureControllerServiceConfigured(); err != nil {
		return nil, err
	}

	startingToken := req.GetStartingToken()
	maxEntries := int(req.GetMaxEntries())

	volumes, nextToken, err := d.store.ListVolumes(startingToken, maxEntries)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "failed to list volumes: %v", err)
	}

	entries := make([]*csi.ListVolumesResponse_Entry, len(volumes))
	for i, vol := range volumes {
		entries[i] = &csi.ListVolumesResponse_Entry{
			Volume: vol.ToCSIVolume(),
		}
	}

	return &csi.ListVolumesResponse{
		Entries:   entries,
		NextToken: nextToken,
	}, nil
}

// GetCapacity returns available capacity
func (d *Driver) GetCapacity(ctx context.Context, req *csi.GetCapacityRequest) (*csi.GetCapacityResponse, error) {
	klog.V(4).Infof("GetCapacity called")

	if err := d.ensureControllerServiceConfigured(); err != nil {
		return nil, err
	}

	// For now, return unlimited capacity
	// In production, this should query ARCA API for actual SVM capacity
	return &csi.GetCapacityResponse{
		AvailableCapacity: 0, // 0 means unknown/unlimited
	}, nil
}

// ControllerGetCapabilities returns controller capabilities
func (d *Driver) ControllerGetCapabilities(ctx context.Context, req *csi.ControllerGetCapabilitiesRequest) (*csi.ControllerGetCapabilitiesResponse, error) {
	klog.V(4).Infof("ControllerGetCapabilities called")

	if err := d.ensureControllerServiceConfigured(); err != nil {
		return nil, err
	}

	capabilities := []csi.ControllerServiceCapability_RPC_Type{
		csi.ControllerServiceCapability_RPC_CREATE_DELETE_VOLUME,
		csi.ControllerServiceCapability_RPC_CREATE_DELETE_SNAPSHOT,
		csi.ControllerServiceCapability_RPC_CLONE_VOLUME,
		csi.ControllerServiceCapability_RPC_EXPAND_VOLUME,
		csi.ControllerServiceCapability_RPC_LIST_VOLUMES,
		csi.ControllerServiceCapability_RPC_LIST_SNAPSHOTS,
	}

	caps := make([]*csi.ControllerServiceCapability, len(capabilities))
	for i, cap := range capabilities {
		caps[i] = &csi.ControllerServiceCapability{
			Type: &csi.ControllerServiceCapability_Rpc{
				Rpc: &csi.ControllerServiceCapability_RPC{
					Type: cap,
				},
			},
		}
	}

	return &csi.ControllerGetCapabilitiesResponse{
		Capabilities: caps,
	}, nil
}

// CreateSnapshot creates a snapshot
func (d *Driver) CreateSnapshot(ctx context.Context, req *csi.CreateSnapshotRequest) (*csi.CreateSnapshotResponse, error) {
	klog.V(4).Infof("CreateSnapshot called with name: %s", req.GetName())

	if err := d.ensureControllerServiceConfigured(); err != nil {
		return nil, err
	}

	if req.GetName() == "" {
		return nil, status.Error(codes.InvalidArgument, "snapshot name is required")
	}

	sourceVolumeID := req.GetSourceVolumeId()
	if sourceVolumeID == "" {
		return nil, status.Error(codes.InvalidArgument, "source volume ID is required")
	}

	// Generate stable snapshot ID (idempotent)
	// Include source volume ID to avoid cross-namespace collisions
	snapshotID := d.snapshotIDGen.GenerateSnapshotID(sourceVolumeID + "/" + req.GetName())

	// Check if snapshot already exists (idempotency)
	existingSnap, err := d.store.GetSnapshot(snapshotID)
	if err == nil {
		klog.V(4).Infof("Snapshot %s already exists, returning existing snapshot", snapshotID)
		return &csi.CreateSnapshotResponse{
			Snapshot: existingSnap.ToCSISnapshot(),
		}, nil
	}
	if !store.IsNotFound(err) {
		return nil, status.Errorf(codes.Internal, "failed to check existing snapshot %s: %v", snapshotID, err)
	}

	// Get source volume info
	sourceVolume, err := d.store.GetVolume(sourceVolumeID)
	if err != nil {
		return nil, status.Errorf(codes.NotFound, "source volume %s not found", sourceVolumeID)
	}

	// Create snapshot path (relative path for consistency)
	snapshotPath := fmt.Sprintf(".snapshots/%s", snapshotID)

	// Create snapshot via ARCA API (server-side reflink)
	klog.V(4).Infof("Creating snapshot %s from volume %s", snapshotID, sourceVolumeID)
	err = d.arcaClient.CreateSnapshot(ctx, &arca.CreateSnapshotRequest{
		SVMName:      sourceVolume.SVMName,
		SourcePath:   sourceVolume.Path,
		SnapshotPath: snapshotPath,
	})
	if err != nil && !arca.IsAlreadyExistsError(err) {
		return nil, status.Errorf(codes.Internal, "failed to create snapshot: %v", err)
	}

	// Store snapshot metadata (initially not ready)
	snapshotInfo := &store.SnapshotInfo{
		SnapshotID:     snapshotID,
		Name:           req.GetName(),
		SourceVolumeID: sourceVolumeID,
		SVMName:        sourceVolume.SVMName,
		Path:           snapshotPath,
		SizeBytes:      sourceVolume.CapacityBytes,
		CreatedAt:      time.Now(),
		ReadyToUse:     false, // Initially false, will be set via status update
	}

	if err := d.store.CreateSnapshot(snapshotInfo); err != nil {
		if store.IsAlreadyExists(err) {
			existingSnap, getErr := d.store.GetSnapshot(snapshotID)
			if getErr == nil {
				return &csi.CreateSnapshotResponse{Snapshot: existingSnap.ToCSISnapshot()}, nil
			}
		}
		return nil, status.Errorf(codes.Internal, "failed to store snapshot metadata: %v", err)
	}

	// Update status to ready (uses /status endpoint which persists correctly)
	if err := d.store.UpdateSnapshotStatus(snapshotID, true); err != nil {
		// Status persistence failed - must return error to maintain consistency
		klog.Errorf("Failed to update snapshot %s status to ready: %v", snapshotID, err)
		// Attempt to clean up the snapshot metadata since ReadyToUse=false is not useful
		if delErr := d.store.DeleteSnapshot(snapshotID); delErr != nil {
			klog.Errorf("Failed to cleanup snapshot metadata after status update failure: %v", delErr)
		}
		return nil, status.Errorf(codes.Internal, "failed to persist snapshot ready status: %v", err)
	}
	// Update our in-memory info to reflect the status
	snapshotInfo.ReadyToUse = true

	klog.Infof("Snapshot %s created successfully from volume %s", snapshotID, sourceVolumeID)

	return &csi.CreateSnapshotResponse{
		Snapshot: snapshotInfo.ToCSISnapshot(),
	}, nil
}

// DeleteSnapshot deletes a snapshot
func (d *Driver) DeleteSnapshot(ctx context.Context, req *csi.DeleteSnapshotRequest) (*csi.DeleteSnapshotResponse, error) {
	klog.V(4).Infof("DeleteSnapshot called with snapshotID: %s", req.GetSnapshotId())

	if err := d.ensureControllerServiceConfigured(); err != nil {
		return nil, err
	}

	snapshotID := req.GetSnapshotId()
	if snapshotID == "" {
		return nil, status.Error(codes.InvalidArgument, "snapshot ID is required")
	}

	// Get snapshot info
	snapshotInfo, err := d.store.GetSnapshot(snapshotID)
	if err != nil {
		if store.IsNotFound(err) {
			// Snapshot doesn't exist in our store - idempotent success
			klog.V(4).Infof("Snapshot %s not found in store, considering it already deleted", snapshotID)
			return &csi.DeleteSnapshotResponse{}, nil
		}
		return nil, status.Errorf(codes.Internal, "failed to get snapshot %s: %v", snapshotID, err)
	}

	// Delete snapshot from ARCA
	klog.V(4).Infof("Deleting snapshot: %s on SVM: %s", snapshotInfo.Path, snapshotInfo.SVMName)
	err = d.arcaClient.DeleteSnapshot(ctx, snapshotInfo.SVMName, snapshotInfo.Path)
	if err != nil && !arca.IsNotFoundError(err) {
		return nil, status.Errorf(codes.Internal, "failed to delete snapshot: %v", err)
	}

	// Delete snapshot metadata - MUST succeed for proper cleanup
	if err := d.store.DeleteSnapshot(snapshotID); err != nil {
		// Only ignore if already deleted (idempotent)
		if !store.IsNotFound(err) {
			return nil, status.Errorf(codes.Internal, "failed to delete snapshot metadata: %v", err)
		}
		klog.V(4).Infof("Snapshot metadata %s already deleted", snapshotID)
	}

	klog.Infof("Snapshot %s deleted successfully", snapshotID)

	return &csi.DeleteSnapshotResponse{}, nil
}

// ListSnapshots lists snapshots with pagination
func (d *Driver) ListSnapshots(ctx context.Context, req *csi.ListSnapshotsRequest) (*csi.ListSnapshotsResponse, error) {
	klog.V(4).Infof("ListSnapshots called")

	if err := d.ensureControllerServiceConfigured(); err != nil {
		return nil, err
	}

	sourceVolumeID := req.GetSourceVolumeId()
	snapshotID := req.GetSnapshotId()
	startingToken := req.GetStartingToken()
	maxEntries := int(req.GetMaxEntries())

	// If specific snapshot ID is requested, return only that snapshot
	if snapshotID != "" {
		snapshot, err := d.store.GetSnapshot(snapshotID)
		if err != nil {
			return nil, status.Errorf(codes.NotFound, "snapshot %s not found", snapshotID)
		}

		return &csi.ListSnapshotsResponse{
			Entries: []*csi.ListSnapshotsResponse_Entry{
				{
					Snapshot: snapshot.ToCSISnapshot(),
				},
			},
		}, nil
	}

	// List snapshots with optional source volume filter
	snapshots, nextToken, err := d.store.ListSnapshots(sourceVolumeID, startingToken, maxEntries)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "failed to list snapshots: %v", err)
	}

	entries := make([]*csi.ListSnapshotsResponse_Entry, len(snapshots))
	for i, snap := range snapshots {
		entries[i] = &csi.ListSnapshotsResponse_Entry{
			Snapshot: snap.ToCSISnapshot(),
		}
	}

	return &csi.ListSnapshotsResponse{
		Entries:   entries,
		NextToken: nextToken,
	}, nil
}

// ControllerExpandVolume expands a volume
func (d *Driver) ControllerExpandVolume(ctx context.Context, req *csi.ControllerExpandVolumeRequest) (*csi.ControllerExpandVolumeResponse, error) {
	klog.V(4).Infof("ControllerExpandVolume called with volumeID: %s", req.GetVolumeId())

	if err := d.ensureControllerServiceConfigured(); err != nil {
		return nil, err
	}

	volumeID := req.GetVolumeId()
	if volumeID == "" {
		return nil, status.Error(codes.InvalidArgument, "volume ID is required")
	}

	if req.GetCapacityRange() == nil {
		return nil, status.Error(codes.InvalidArgument, "capacity range is required")
	}

	newCapacityBytes := req.GetCapacityRange().GetRequiredBytes()
	if newCapacityBytes == 0 {
		return nil, status.Error(codes.InvalidArgument, "required bytes must be greater than 0")
	}

	// Get volume info
	volumeInfo, err := d.store.GetVolume(volumeID)
	if err != nil {
		return nil, status.Errorf(codes.NotFound, "volume %s not found", volumeID)
	}

	// Check if expansion is needed
	if newCapacityBytes <= volumeInfo.CapacityBytes {
		klog.V(4).Infof("Volume %s already has capacity >= %d bytes, no expansion needed", volumeID, newCapacityBytes)
		return &csi.ControllerExpandVolumeResponse{
			CapacityBytes:         volumeInfo.CapacityBytes,
			NodeExpansionRequired: false,
		}, nil
	}

	// Expand quota via ARCA API
	klog.V(4).Infof("Expanding quota for volume %s to %d bytes", volumeID, newCapacityBytes)
	err = d.arcaClient.SetQuota(ctx, &arca.SetQuotaRequest{
		SVMName:    volumeInfo.SVMName,
		Path:       volumeInfo.Path,
		QuotaBytes: newCapacityBytes,
	})
	if err != nil {
		return nil, status.Errorf(codes.Internal, "failed to expand quota: %v", err)
	}

	// Update volume metadata
	volumeInfo.CapacityBytes = newCapacityBytes
	if err := d.store.UpdateVolume(volumeInfo); err != nil {
		klog.Warningf("Failed to update volume metadata for %s: %v", volumeID, err)
		// Continue anyway - the quota is already expanded
	}

	klog.Infof("Volume %s expanded successfully to %d bytes", volumeID, newCapacityBytes)

	return &csi.ControllerExpandVolumeResponse{
		CapacityBytes:         newCapacityBytes,
		NodeExpansionRequired: false, // NFS doesn't require node-side expansion
	}, nil
}

// ControllerGetVolume returns volume information
func (d *Driver) ControllerGetVolume(ctx context.Context, req *csi.ControllerGetVolumeRequest) (*csi.ControllerGetVolumeResponse, error) {
	return nil, status.Error(codes.Unimplemented, "ControllerGetVolume is not implemented")
}

// validateVolumeCapabilities validates requested volume capabilities
func (d *Driver) validateVolumeCapabilities(caps []*csi.VolumeCapability) error {
	for _, cap := range caps {
		// Check access mode
		mode := cap.GetAccessMode()
		if mode == nil {
			return fmt.Errorf("access mode is required")
		}

		switch mode.GetMode() {
		case csi.VolumeCapability_AccessMode_SINGLE_NODE_WRITER,
			csi.VolumeCapability_AccessMode_SINGLE_NODE_READER_ONLY,
			csi.VolumeCapability_AccessMode_MULTI_NODE_READER_ONLY,
			csi.VolumeCapability_AccessMode_MULTI_NODE_MULTI_WRITER:
			// Supported modes
		default:
			return fmt.Errorf("unsupported access mode: %v", mode.GetMode())
		}

		// Check access type
		accessType := cap.GetAccessType()
		if accessType == nil {
			return fmt.Errorf("access type is required")
		}

		switch accessType.(type) {
		case *csi.VolumeCapability_Mount:
			// Mount access type is supported
		case *csi.VolumeCapability_Block:
			return fmt.Errorf("block access type is not supported")
		default:
			return fmt.Errorf("unknown access type")
		}
	}

	return nil
}
