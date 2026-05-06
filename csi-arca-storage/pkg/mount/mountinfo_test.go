package mount

import (
	"strings"
	"testing"
)

func TestValidateMountSourceFromEntriesAcceptsNFSSource(t *testing.T) {
	entries := mustParseMountInfo(t, `
36 25 0:42 / /var/lib/kubelet/plugins/csi.arca-storage.io/mounts/tenant-a rw,relatime - nfs4 192.0.2.10:/exports/tenant-a rw
`)

	if err := validateMountSourceFromEntries(
		entries,
		"/var/lib/kubelet/plugins/csi.arca-storage.io/mounts/tenant-a",
		"192.0.2.10:/exports/tenant-a",
	); err != nil {
		t.Fatalf("expected NFS source to be accepted: %v", err)
	}
}

func TestValidateMountSourceFromEntriesRejectsWrongNFSSource(t *testing.T) {
	entries := mustParseMountInfo(t, `
36 25 0:42 / /var/lib/kubelet/plugins/csi.arca-storage.io/mounts/tenant-a rw,relatime - nfs4 192.0.2.99:/exports/tenant-a rw
`)

	err := validateMountSourceFromEntries(
		entries,
		"/var/lib/kubelet/plugins/csi.arca-storage.io/mounts/tenant-a",
		"192.0.2.10:/exports/tenant-a",
	)
	if err == nil {
		t.Fatal("expected wrong NFS source to be rejected")
	}
	if !strings.Contains(err.Error(), "mount source mismatch") {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestValidateMountSourceFromEntriesAcceptsBindRoot(t *testing.T) {
	entries := mustParseMountInfo(t, `
36 25 0:42 / /var/lib/kubelet/plugins/csi.arca-storage.io/mounts/tenant-a rw,relatime - nfs4 192.0.2.10:/exports/tenant-a rw
37 25 0:42 /volumes/vol-a /var/lib/kubelet/plugins/kubernetes.io/csi/pv/vol-a/globalmount rw,relatime - nfs4 192.0.2.10:/exports/tenant-a rw
38 25 0:42 /volumes/vol-a /var/lib/kubelet/pods/pod-a/volumes/kubernetes.io~csi/vol-a/mount rw,relatime - nfs4 192.0.2.10:/exports/tenant-a rw
`)

	if err := validateMountSourceFromEntries(
		entries,
		"/var/lib/kubelet/plugins/kubernetes.io/csi/pv/vol-a/globalmount",
		"/var/lib/kubelet/plugins/csi.arca-storage.io/mounts/tenant-a/volumes/vol-a",
	); err != nil {
		t.Fatalf("expected staging bind source to be accepted: %v", err)
	}

	if err := validateMountSourceFromEntries(
		entries,
		"/var/lib/kubelet/pods/pod-a/volumes/kubernetes.io~csi/vol-a/mount",
		"/var/lib/kubelet/plugins/kubernetes.io/csi/pv/vol-a/globalmount",
	); err != nil {
		t.Fatalf("expected publish bind source to be accepted: %v", err)
	}
}

func TestValidateMountSourceFromEntriesRejectsWrongBindRoot(t *testing.T) {
	entries := mustParseMountInfo(t, `
36 25 0:42 / /var/lib/kubelet/plugins/csi.arca-storage.io/mounts/tenant-a rw,relatime - nfs4 192.0.2.10:/exports/tenant-a rw
37 25 0:42 /volumes/vol-b /var/lib/kubelet/plugins/kubernetes.io/csi/pv/vol-a/globalmount rw,relatime - nfs4 192.0.2.10:/exports/tenant-a rw
`)

	err := validateMountSourceFromEntries(
		entries,
		"/var/lib/kubelet/plugins/kubernetes.io/csi/pv/vol-a/globalmount",
		"/var/lib/kubelet/plugins/csi.arca-storage.io/mounts/tenant-a/volumes/vol-a",
	)
	if err == nil {
		t.Fatal("expected wrong bind root to be rejected")
	}
	if !strings.Contains(err.Error(), "mount source mismatch") {
		t.Fatalf("unexpected error: %v", err)
	}
}

func mustParseMountInfo(t *testing.T, content string) []mountInfoEntry {
	t.Helper()
	entries, err := parseMountInfo(strings.TrimSpace(content))
	if err != nil {
		t.Fatalf("parseMountInfo failed: %v", err)
	}
	return entries
}
