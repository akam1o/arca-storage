package driver

import (
	"reflect"
	"testing"

	"github.com/container-storage-interface/spec/lib/go/csi"
)

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
