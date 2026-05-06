package mount

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	mountutils "k8s.io/mount-utils"
)

func newTestMountManager(t *testing.T) *MountManager {
	t.Helper()
	return &MountManager{
		mounts:        make(map[string]*SVMMount),
		nodeState:     &NodeState{data: &NodeStateData{Volumes: make(map[string]*VolumeStaging)}},
		baseMountPath: t.TempDir(),
		mounter:       mountutils.NewFakeMounter(nil),
		validator:     &fakeMountSourceValidator{},
	}
}

type fakeMountSourceValidator struct {
	err   error
	calls []mountSourceValidationCall
}

type mountSourceValidationCall struct {
	targetPath     string
	expectedSource string
}

func (v *fakeMountSourceValidator) ValidateMountSource(targetPath, expectedSource string) error {
	v.calls = append(v.calls, mountSourceValidationCall{
		targetPath:     targetPath,
		expectedSource: expectedSource,
	})
	return v.err
}

func TestEnsureSVMMountRejectsConflictingOptions(t *testing.T) {
	manager := newTestMountManager(t)
	ctx := context.Background()
	initialOptions := MergeNFSMountOptions([]string{"nconnect=8"})

	if _, err := manager.EnsureSVMMount(ctx, "tenant-a", "192.0.2.10", initialOptions); err != nil {
		t.Fatalf("initial mount failed: %v", err)
	}
	if _, err := manager.EnsureSVMMount(ctx, "tenant-a", "192.0.2.10", []string{"nconnect=8"}); err != nil {
		t.Fatalf("same options should be accepted: %v", err)
	}

	_, err := manager.EnsureSVMMount(ctx, "tenant-a", "192.0.2.10", []string{"nconnect=16"})
	if err == nil {
		t.Fatal("expected conflicting options to be rejected")
	}
	if !strings.Contains(err.Error(), "different NFS options") {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestEnsureSVMMountRejectsWrongExistingSource(t *testing.T) {
	validator := &fakeMountSourceValidator{
		err: fmt.Errorf("mount source mismatch: active=192.0.2.99:/exports/tenant-a requested=192.0.2.10:/exports/tenant-a"),
	}
	manager := &MountManager{
		mounts:        make(map[string]*SVMMount),
		nodeState:     &NodeState{data: &NodeStateData{Volumes: make(map[string]*VolumeStaging)}},
		baseMountPath: t.TempDir(),
		mounter:       mountutils.NewFakeMounter(nil),
		validator:     validator,
	}
	mountPath := manager.getMountPath("tenant-a")
	if err := os.MkdirAll(mountPath, 0750); err != nil {
		t.Fatalf("failed to create mount path: %v", err)
	}
	if err := manager.mounter.Mount("192.0.2.99:/exports/tenant-a", mountPath, "nfs4", nil); err != nil {
		t.Fatalf("failed to seed fake mount: %v", err)
	}
	manager.mounts["tenant-a"] = &SVMMount{
		SVMName:   "tenant-a",
		VIP:       "192.0.2.99",
		MountPath: mountPath,
	}

	_, err := manager.EnsureSVMMount(context.Background(), "tenant-a", "192.0.2.10", nil)
	if err == nil {
		t.Fatal("expected wrong existing source to be rejected")
	}
	if !strings.Contains(err.Error(), "not safe to reuse") {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(validator.calls) != 1 || validator.calls[0].expectedSource != "192.0.2.10:/exports/tenant-a" {
		t.Fatalf("unexpected validator calls: %#v", validator.calls)
	}
}

func TestGetUniqueSVMMountsNormalizesLegacyOptions(t *testing.T) {
	nodeState := &NodeState{data: &NodeStateData{Volumes: map[string]*VolumeStaging{
		"volume-a": {
			SVMName:         "tenant-a",
			VIP:             "192.0.2.10",
			NFSMountOptions: []string{"nconnect=8"},
		},
	}}}

	got, err := nodeState.GetUniqueSVMMounts()
	if err != nil {
		t.Fatalf("GetUniqueSVMMounts failed: %v", err)
	}
	want := MergeNFSMountOptions([]string{"nconnect=8"})
	if !reflect.DeepEqual(got["tenant-a"].NFSMountOptions, want) {
		t.Fatalf("unexpected normalized options: got %v want %v", got["tenant-a"].NFSMountOptions, want)
	}
}

func TestGetUniqueSVMMountsRejectsConflictingOptions(t *testing.T) {
	nodeState := &NodeState{data: &NodeStateData{Volumes: map[string]*VolumeStaging{
		"volume-a": {
			SVMName:         "tenant-a",
			VIP:             "192.0.2.10",
			NFSMountOptions: []string{"nconnect=8"},
		},
		"volume-b": {
			SVMName:         "tenant-a",
			VIP:             "192.0.2.10",
			NFSMountOptions: []string{"nconnect=16"},
		},
	}}}

	_, err := nodeState.GetUniqueSVMMounts()
	if err == nil {
		t.Fatal("expected conflicting options to be rejected")
	}
}

func TestValidateVolumeStagingDetectsMismatchedVolumePath(t *testing.T) {
	state, err := NewNodeState(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewNodeState failed: %v", err)
	}
	if err := state.RecordVolumeStaging(
		"volume-a",
		"tenant-a",
		"192.0.2.10",
		"pvc-a",
		"/stage/volume-a",
		[]string{"nconnect=8"},
	); err != nil {
		t.Fatalf("RecordVolumeStaging failed: %v", err)
	}

	err = state.ValidateVolumeStaging(
		"volume-a",
		"tenant-a",
		"192.0.2.10",
		"pvc-b",
		"/stage/volume-a",
		MergeNFSMountOptions([]string{"nconnect=8"}),
	)
	if err == nil {
		t.Fatal("expected volume path mismatch to be rejected")
	}
	if !strings.Contains(err.Error(), "path mismatch") {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestHasVolumePublishRequiresRecordedTarget(t *testing.T) {
	state, err := NewNodeState(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewNodeState failed: %v", err)
	}
	if err := state.RecordVolumeStaging(
		"volume-a",
		"tenant-a",
		"192.0.2.10",
		"pvc-a",
		"/stage/volume-a",
		nil,
	); err != nil {
		t.Fatalf("RecordVolumeStaging failed: %v", err)
	}

	if state.HasVolumePublish("volume-a", "/pods/volume-a") {
		t.Fatal("target should not be recorded before RecordVolumePublish")
	}
	if err := state.RecordVolumePublish("volume-a", "/pods/volume-a"); err != nil {
		t.Fatalf("RecordVolumePublish failed: %v", err)
	}
	if !state.HasVolumePublish("volume-a", "/pods/volume-a") {
		t.Fatal("target should be recorded after RecordVolumePublish")
	}
}
