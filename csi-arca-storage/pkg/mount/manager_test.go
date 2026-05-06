package mount

import (
	"context"
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
	}
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
