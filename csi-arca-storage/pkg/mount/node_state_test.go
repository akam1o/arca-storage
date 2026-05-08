package mount

import (
	"path/filepath"
	"testing"
)

func TestNodeStateRecordVolumeStagingPreservesExternalUpdates(t *testing.T) {
	stateFile := filepath.Join(t.TempDir(), "state.json")
	first, err := NewNodeState(stateFile)
	if err != nil {
		t.Fatalf("NewNodeState(first) failed: %v", err)
	}
	second, err := NewNodeState(stateFile)
	if err != nil {
		t.Fatalf("NewNodeState(second) failed: %v", err)
	}

	if err := first.RecordVolumeStaging(
		"volume-a",
		"tenant-a",
		"192.0.2.10",
		"",
		"pvc-a",
		"/stage/volume-a",
		nil,
	); err != nil {
		t.Fatalf("RecordVolumeStaging(first) failed: %v", err)
	}
	if err := second.RecordVolumeStaging(
		"volume-b",
		"tenant-b",
		"192.0.2.11",
		"",
		"pvc-b",
		"/stage/volume-b",
		nil,
	); err != nil {
		t.Fatalf("RecordVolumeStaging(second) failed: %v", err)
	}

	reloaded, err := NewNodeState(stateFile)
	if err != nil {
		t.Fatalf("NewNodeState(reloaded) failed: %v", err)
	}
	staged := reloaded.GetStagedVolumes()
	if len(staged) != 2 {
		t.Fatalf("staged volume count = %d, want 2", len(staged))
	}
	if staged["volume-a"] == nil || staged["volume-b"] == nil {
		t.Fatalf("staged volumes = %#v, want volume-a and volume-b", staged)
	}
}
