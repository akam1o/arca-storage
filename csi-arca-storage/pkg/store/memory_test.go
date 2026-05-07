package store

import "testing"

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
