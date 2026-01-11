package arca

import "time"

// SVM represents an ARCA Storage Virtual Machine
type SVM struct {
	Name      string    `json:"name"`
	VLANID    int       `json:"vlan_id"`
	IPCIDR    string    `json:"ip_cidr"`
	VIP       string    `json:"vip"`
	Gateway   string    `json:"gateway"`
	MTU       int       `json:"mtu"`
	State     string    `json:"state"`
	CreatedAt time.Time `json:"created_at"`
}

// CreateSVMRequest represents a request to create an SVM
type CreateSVMRequest struct {
	Name    string `json:"name"`
	VLANID  int    `json:"vlan_id"`
	IPCIDR  string `json:"ip_cidr"`
	Gateway string `json:"gateway"`
	MTU     int    `json:"mtu"`
}

// CreateDirectoryRequest represents a request to create a directory
type CreateDirectoryRequest struct {
	SVMName    string `json:"svm_name"`
	Path       string `json:"path"`
	QuotaBytes int64  `json:"quota_bytes,omitempty"`
}

// CreateSnapshotRequest represents a request to create a snapshot
type CreateSnapshotRequest struct {
	SVMName      string `json:"svm_name"`
	SourcePath   string `json:"source_path"`
	SnapshotPath string `json:"snapshot_path"`
}

// RestoreSnapshotRequest represents a request to restore from snapshot
type RestoreSnapshotRequest struct {
	SVMName      string `json:"svm_name"`
	SnapshotPath string `json:"snapshot_path"`
	TargetPath   string `json:"target_path"`
}

// SetQuotaRequest represents a request to set XFS project quota
type SetQuotaRequest struct {
	SVMName    string `json:"svm_name"`
	Path       string `json:"path"`
	QuotaBytes int64  `json:"quota_bytes"`
}

// ExpandQuotaRequest represents a request to expand quota
type ExpandQuotaRequest struct {
	SVMName       string `json:"svm_name"`
	Path          string `json:"path"`
	NewQuotaBytes int64  `json:"new_quota_bytes"`
}

// QuotaInfo represents quota usage information
type QuotaInfo struct {
	Path       string `json:"path"`
	QuotaBytes int64  `json:"quota_bytes"`
	UsedBytes  int64  `json:"used_bytes"`
	ProjectID  int    `json:"project_id"`
}

// NetworkAllocation represents allocated network parameters
type NetworkAllocation struct {
	VLANID  int    `json:"vlan_id"`
	IPCIDR  string `json:"ip_cidr"`
	Gateway string `json:"gateway"`
}

// APIResponse represents a generic API response wrapper
type APIResponse struct {
	Data    interface{} `json:"data,omitempty"`
	Error   string      `json:"error,omitempty"`
	Message string      `json:"message,omitempty"`
}

// SVMListResponse represents a list of SVMs
type SVMListResponse struct {
	SVMs []SVM `json:"svms"`
}

// CapacityInfo represents SVM capacity information
type CapacityInfo struct {
	TotalBytes     int64 `json:"total_bytes"`
	AvailableBytes int64 `json:"available_bytes"`
	UsedBytes      int64 `json:"used_bytes"`
}
