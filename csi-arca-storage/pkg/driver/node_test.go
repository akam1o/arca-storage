package driver

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/container-storage-interface/spec/lib/go/csi"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	mountutils "k8s.io/mount-utils"

	arcamount "github.com/akam1o/csi-arca-storage/pkg/mount"
)

type fakeMountSourceValidator struct {
	err error
}

func (v fakeMountSourceValidator) ValidateMountSource(targetPath, expectedSource string) error {
	return v.err
}

func TestNFSMountOptionsFromCapabilityUsesMountFlags(t *testing.T) {
	capability := &csi.VolumeCapability{
		AccessType: &csi.VolumeCapability_Mount{
			Mount: &csi.VolumeCapability_MountVolume{
				MountFlags: []string{"nfsvers=4.1", "rsize=262144", "ro", "bind", "remount", "rw", "soft", "resvport"},
			},
		},
	}

	got := nfsMountOptionsFromCapability(capability)
	want := []string{"vers=4.1", "rsize=262144", "wsize=1048576", "soft", "timeo=600", "retrans=2", "resvport"}

	if !reflect.DeepEqual(got, want) {
		t.Fatalf("unexpected NFS mount options: got %v want %v", got, want)
	}
}

func TestNFSMountOptionsFromCapabilityMergesCustomFlagsWithDefaults(t *testing.T) {
	capability := &csi.VolumeCapability{
		AccessType: &csi.VolumeCapability_Mount{
			Mount: &csi.VolumeCapability_MountVolume{
				MountFlags: []string{"nconnect=8"},
			},
		},
	}

	got := nfsMountOptionsFromCapability(capability)
	want := []string{"vers=4.2", "rsize=1048576", "wsize=1048576", "hard", "timeo=600", "retrans=2", "noresvport", "nconnect=8"}

	if !reflect.DeepEqual(got, want) {
		t.Fatalf("unexpected NFS mount options: got %v want %v", got, want)
	}
}

func TestNFSMountOptionsFromCapabilityDefaultsWhenNoFlags(t *testing.T) {
	capability := &csi.VolumeCapability{
		AccessType: &csi.VolumeCapability_Mount{
			Mount: &csi.VolumeCapability_MountVolume{},
		},
	}

	got := nfsMountOptionsFromCapability(capability)
	if len(got) == 0 {
		t.Fatal("expected default NFS mount options")
	}
}

func TestBindMountOptionsStayLocalOnly(t *testing.T) {
	if got, want := bindMountOptions(), []string{"bind"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("unexpected bind mount options: got %v want %v", got, want)
	}

	if got, want := readonlyBindRemountOptions(), []string{"bind", "remount", "ro"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("unexpected read-only remount options: got %v want %v", got, want)
	}
}

func TestValidateSVMNameRejectsPathTraversal(t *testing.T) {
	for _, name := range []string{"../tenant", "tenant/a", "tenant a", "-tenant"} {
		if err := validateSVMName(name); err == nil {
			t.Fatalf("expected invalid SVM name %q to be rejected", name)
		}
	}

	if err := validateSVMName("tenant-a_1.example"); err != nil {
		t.Fatalf("expected valid SVM name to be accepted: %v", err)
	}
}

func TestNodePublishRejectsUnrecordedExistingMount(t *testing.T) {
	tmp := t.TempDir()
	targetPath := filepath.Join(tmp, "target")
	stagingPath := filepath.Join(tmp, "staging")
	if err := os.MkdirAll(targetPath, 0750); err != nil {
		t.Fatalf("failed to create target path: %v", err)
	}
	if err := os.MkdirAll(stagingPath, 0750); err != nil {
		t.Fatalf("failed to create staging path: %v", err)
	}
	mountedTargetPath, err := filepath.EvalSymlinks(targetPath)
	if err != nil {
		t.Fatalf("failed to resolve target path: %v", err)
	}
	mountedStagingPath, err := filepath.EvalSymlinks(stagingPath)
	if err != nil {
		t.Fatalf("failed to resolve staging path: %v", err)
	}

	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	fakeMounter := mountutils.NewFakeMounter([]mountutils.MountPoint{
		{Device: mountedStagingPath, Path: mountedTargetPath, Type: "", Opts: []string{"bind"}},
	})
	driver := &Driver{
		mode:                 "node",
		nodeID:               "node-a",
		nodeState:            nodeState,
		mountManager:         new(arcamount.MountManager),
		nodeMounter:          fakeMounter,
		mountSourceValidator: fakeMountSourceValidator{},
	}

	_, err = driver.NodePublishVolume(context.Background(), &csi.NodePublishVolumeRequest{
		VolumeId:          "vol-a",
		StagingTargetPath: stagingPath,
		TargetPath:        targetPath,
		VolumeCapability: &csi.VolumeCapability{
			AccessType: &csi.VolumeCapability_Mount{Mount: &csi.VolumeCapability_MountVolume{}},
			AccessMode: &csi.VolumeCapability_AccessMode{Mode: csi.VolumeCapability_AccessMode_SINGLE_NODE_WRITER},
		},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("expected FailedPrecondition, got %v", err)
	}
	if !strings.Contains(err.Error(), "not recorded for volume") {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestNodePublishRejectsExistingMountWithDifferentSource(t *testing.T) {
	tmp := t.TempDir()
	targetPath := filepath.Join(tmp, "target")
	stagingPath := filepath.Join(tmp, "staging")
	if err := os.MkdirAll(targetPath, 0750); err != nil {
		t.Fatalf("failed to create target path: %v", err)
	}
	if err := os.MkdirAll(stagingPath, 0750); err != nil {
		t.Fatalf("failed to create staging path: %v", err)
	}
	mountedTargetPath, err := filepath.EvalSymlinks(targetPath)
	if err != nil {
		t.Fatalf("failed to resolve target path: %v", err)
	}

	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	if err := nodeState.RecordVolumeStaging("vol-a", "svm-a", "10.0.0.1", "", "volumes/vol-a", stagingPath, nil); err != nil {
		t.Fatalf("failed to record staging: %v", err)
	}
	if err := nodeState.RecordVolumePublish("vol-a", targetPath); err != nil {
		t.Fatalf("failed to record publish: %v", err)
	}

	fakeMounter := mountutils.NewFakeMounter([]mountutils.MountPoint{
		{Device: "/var/lib/kubelet/plugins/stale-volume", Path: mountedTargetPath, Type: "", Opts: []string{"bind"}},
	})
	driver := &Driver{
		mode:                 "node",
		nodeID:               "node-a",
		nodeState:            nodeState,
		mountManager:         new(arcamount.MountManager),
		nodeMounter:          fakeMounter,
		mountSourceValidator: fakeMountSourceValidator{err: fmt.Errorf("mount source mismatch: active=stale requested=%s", stagingPath)},
	}

	_, err = driver.NodePublishVolume(context.Background(), &csi.NodePublishVolumeRequest{
		VolumeId:          "vol-a",
		StagingTargetPath: stagingPath,
		TargetPath:        targetPath,
		VolumeCapability: &csi.VolumeCapability{
			AccessType: &csi.VolumeCapability_Mount{Mount: &csi.VolumeCapability_MountVolume{}},
			AccessMode: &csi.VolumeCapability_AccessMode{Mode: csi.VolumeCapability_AccessMode_SINGLE_NODE_WRITER},
		},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("expected FailedPrecondition, got %v", err)
	}
	if !strings.Contains(err.Error(), "mount source mismatch") {
		t.Fatalf("unexpected error: %v", err)
	}
}
