package mount

import (
	"fmt"

	"k8s.io/mount-utils"
)

// NFSMounter provides NFS mount operations
type NFSMounter struct {
	mounter mount.Interface
}

// NewNFSMounter creates a new NFS mounter
func NewNFSMounter() *NFSMounter {
	return &NFSMounter{
		mounter: mount.New(""),
	}
}

// Mount performs an NFS mount
func (m *NFSMounter) Mount(source, target string, options []string) error {
	return m.mounter.Mount(source, target, "nfs4", options)
}

// Unmount unmounts a path
func (m *NFSMounter) Unmount(target string) error {
	return m.mounter.Unmount(target)
}

// IsLikelyNotMountPoint checks if a path is likely not a mount point
func (m *NFSMounter) IsLikelyNotMountPoint(path string) (bool, error) {
	return m.mounter.IsLikelyNotMountPoint(path)
}

// BindMount performs a bind mount
func (m *NFSMounter) BindMount(source, target string, readonly bool) error {
	options := []string{"bind"}
	if readonly {
		options = append(options, "ro")
	}

	return m.mounter.Mount(source, target, "", options)
}

// GetDefaultNFSOptions returns default NFS mount options
func GetDefaultNFSOptions() []string {
	return []string{
		"vers=4.2",
		"rsize=1048576",
		"wsize=1048576",
		"hard",
		"timeo=600",
		"retrans=2",
		"noresvport",
	}
}

// FormatNFSSource formats an NFS source string
func FormatNFSSource(vip, exportPath string) string {
	return fmt.Sprintf("%s:%s", vip, exportPath)
}
