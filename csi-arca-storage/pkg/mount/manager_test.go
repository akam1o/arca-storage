package mount

import (
	"context"
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
