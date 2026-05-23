package store

import (
	"strings"
	"testing"
)

func TestMemoryStoreListVolumesStablePagination(t *testing.T) {
	st := NewMemoryStore()
	for _, volumeID := range []string{"vol-b", "vol-a", "vol-c"} {
		if err := st.CreateVolume(&VolumeInfo{VolumeID: volumeID}); err != nil {
			t.Fatalf("CreateVolume(%q) error = %v", volumeID, err)
		}
	}

	page, nextToken, err := st.ListVolumes("", 2)
	if err != nil {
		t.Fatalf("ListVolumes() error = %v", err)
	}
	assertVolumeIDs(t, page, []string{"vol-a", "vol-b"})
	if nextToken != "vol-b" {
		t.Fatalf("nextToken = %q, want vol-b", nextToken)
	}

	page, nextToken, err = st.ListVolumes(nextToken, 2)
	if err != nil {
		t.Fatalf("ListVolumes(second page) error = %v", err)
	}
	assertVolumeIDs(t, page, []string{"vol-c"})
	if nextToken != "" {
		t.Fatalf("second nextToken = %q, want empty", nextToken)
	}
}

func TestMemoryStoreListVolumesInvalidTokenDoesNotEchoToken(t *testing.T) {
	st := NewMemoryStore()
	if err := st.CreateVolume(&VolumeInfo{VolumeID: "vol-a"}); err != nil {
		t.Fatalf("CreateVolume() error = %v", err)
	}

	const token = "secret-volume-token"
	_, _, err := st.ListVolumes(token, 1)
	if !IsNotFound(err) {
		t.Fatalf("ListVolumes() error = %v, want not found", err)
	}
	if strings.Contains(err.Error(), token) {
		t.Fatalf("ListVolumes() error %q contains starting token", err)
	}
}

func TestMemoryStoreUpdateVolumePreservesLargerCapacity(t *testing.T) {
	st := NewMemoryStore()
	if err := st.CreateVolume(&VolumeInfo{VolumeID: "vol-a", CapacityBytes: 20 << 30}); err != nil {
		t.Fatalf("CreateVolume() error = %v", err)
	}

	stale, err := st.GetVolume("vol-a")
	if err != nil {
		t.Fatalf("GetVolume() error = %v", err)
	}
	stale.CapacityBytes = 10 << 30

	if err := st.UpdateVolume(stale); err != nil {
		t.Fatalf("UpdateVolume() error = %v", err)
	}

	stored, err := st.GetVolume("vol-a")
	if err != nil {
		t.Fatalf("GetVolume(updated) error = %v", err)
	}
	if stored.CapacityBytes != 20<<30 {
		t.Fatalf("capacity = %d, want %d", stored.CapacityBytes, int64(20<<30))
	}
}

func TestMemoryStoreListVolumesReturnsCopies(t *testing.T) {
	st := NewMemoryStore()
	if err := st.CreateVolume(&VolumeInfo{VolumeID: "vol-a", CapacityBytes: 20 << 30}); err != nil {
		t.Fatalf("CreateVolume() error = %v", err)
	}

	volumes, _, err := st.ListVolumes("", 0)
	if err != nil {
		t.Fatalf("ListVolumes() error = %v", err)
	}
	volumes[0].CapacityBytes = 10 << 30

	stored, err := st.GetVolume("vol-a")
	if err != nil {
		t.Fatalf("GetVolume() error = %v", err)
	}
	if stored.CapacityBytes != 20<<30 {
		t.Fatalf("capacity = %d, want %d", stored.CapacityBytes, int64(20<<30))
	}
}

func TestMemoryStoreListSnapshotsStableFilteredPagination(t *testing.T) {
	st := NewMemoryStore()
	for _, snapshot := range []*SnapshotInfo{
		{SnapshotID: "snap-b", SourceVolumeID: "vol-a"},
		{SnapshotID: "snap-c", SourceVolumeID: "vol-b"},
		{SnapshotID: "snap-a", SourceVolumeID: "vol-a"},
	} {
		if err := st.CreateSnapshot(snapshot); err != nil {
			t.Fatalf("CreateSnapshot(%q) error = %v", snapshot.SnapshotID, err)
		}
	}

	page, nextToken, err := st.ListSnapshots("vol-a", "", 1)
	if err != nil {
		t.Fatalf("ListSnapshots() error = %v", err)
	}
	assertSnapshotIDs(t, page, []string{"snap-a"})
	if nextToken != "snap-a" {
		t.Fatalf("nextToken = %q, want snap-a", nextToken)
	}

	page, nextToken, err = st.ListSnapshots("vol-a", nextToken, 1)
	if err != nil {
		t.Fatalf("ListSnapshots(second page) error = %v", err)
	}
	assertSnapshotIDs(t, page, []string{"snap-b"})
	if nextToken != "" {
		t.Fatalf("second nextToken = %q, want empty", nextToken)
	}
}

func TestMemoryStoreListSnapshotsInvalidTokenDoesNotEchoToken(t *testing.T) {
	st := NewMemoryStore()
	if err := st.CreateSnapshot(&SnapshotInfo{
		SnapshotID:     "snap-a",
		SourceVolumeID: "vol-a",
	}); err != nil {
		t.Fatalf("CreateSnapshot() error = %v", err)
	}

	const token = "secret-snapshot-token"
	_, _, err := st.ListSnapshots("vol-a", token, 1)
	if !IsNotFound(err) {
		t.Fatalf("ListSnapshots() error = %v, want not found", err)
	}
	if strings.Contains(err.Error(), token) {
		t.Fatalf("ListSnapshots() error %q contains starting token", err)
	}
}

func TestMemoryStoreSnapshotsReturnCopies(t *testing.T) {
	st := NewMemoryStore()
	snapshot := &SnapshotInfo{
		SnapshotID:     "snap-a",
		SourceVolumeID: "vol-a",
		SizeBytes:      20 << 30,
	}
	if err := st.CreateSnapshot(snapshot); err != nil {
		t.Fatalf("CreateSnapshot() error = %v", err)
	}
	snapshot.SizeBytes = 10 << 30

	stored, err := st.GetSnapshot("snap-a")
	if err != nil {
		t.Fatalf("GetSnapshot() error = %v", err)
	}
	if stored.SizeBytes != 20<<30 {
		t.Fatalf("stored size after input mutation = %d, want %d", stored.SizeBytes, int64(20<<30))
	}

	stored.SizeBytes = 5 << 30
	storedAgain, err := st.GetSnapshot("snap-a")
	if err != nil {
		t.Fatalf("GetSnapshot(second) error = %v", err)
	}
	if storedAgain.SizeBytes != 20<<30 {
		t.Fatalf("stored size after get mutation = %d, want %d", storedAgain.SizeBytes, int64(20<<30))
	}

	snapshots, _, err := st.ListSnapshots("", "", 0)
	if err != nil {
		t.Fatalf("ListSnapshots() error = %v", err)
	}
	snapshots[0].SizeBytes = 1 << 30
	storedAgain, err = st.GetSnapshot("snap-a")
	if err != nil {
		t.Fatalf("GetSnapshot(after list) error = %v", err)
	}
	if storedAgain.SizeBytes != 20<<30 {
		t.Fatalf("stored size after list mutation = %d, want %d", storedAgain.SizeBytes, int64(20<<30))
	}
}

func assertVolumeIDs(t *testing.T, volumes []*VolumeInfo, want []string) {
	t.Helper()
	if len(volumes) != len(want) {
		t.Fatalf("len(volumes) = %d, want %d", len(volumes), len(want))
	}
	for i, volume := range volumes {
		if volume.VolumeID != want[i] {
			t.Fatalf("volumes[%d].VolumeID = %q, want %q", i, volume.VolumeID, want[i])
		}
	}
}

func assertSnapshotIDs(t *testing.T, snapshots []*SnapshotInfo, want []string) {
	t.Helper()
	if len(snapshots) != len(want) {
		t.Fatalf("len(snapshots) = %d, want %d", len(snapshots), len(want))
	}
	for i, snapshot := range snapshots {
		if snapshot.SnapshotID != want[i] {
			t.Fatalf("snapshots[%d].SnapshotID = %q, want %q", i, snapshot.SnapshotID, want[i])
		}
	}
}
