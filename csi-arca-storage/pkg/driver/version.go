package driver

const (
	// DriverName is the name of the CSI driver
	DriverName = "csi.arca-storage.io"

	// DriverVersion is the version of the CSI driver
	DriverVersion = "v0.1.0"

	// DefaultStateFilePath is the default path for node state file
	DefaultStateFilePath = "/var/lib/csi-arca-storage/node-volumes.json"

	// DefaultBaseMountPath is the default base path for SVM mounts
	DefaultBaseMountPath = "/var/lib/kubelet/plugins/csi.arca-storage.io/mounts"
)
