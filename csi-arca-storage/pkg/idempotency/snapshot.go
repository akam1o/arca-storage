package idempotency

import (
	"crypto/sha256"
	"encoding/hex"
)

// SnapshotIDGenerator generates stable snapshot IDs from snapshot names
type SnapshotIDGenerator struct{}

// NewSnapshotIDGenerator creates a new snapshot ID generator
func NewSnapshotIDGenerator() *SnapshotIDGenerator {
	return &SnapshotIDGenerator{}
}

// GenerateSnapshotID creates a deterministic snapshot ID from request name
// Format: {hash(name)[:16]} (64-bit hash, NO "snap-" prefix here)
// The "snap-" prefix is added when constructing the full path
func (g *SnapshotIDGenerator) GenerateSnapshotID(name string) string {
	h := sha256.Sum256([]byte(name))
	return hex.EncodeToString(h[:8])
}

// ValidateSnapshotID checks if a snapshot ID has the correct format
func (g *SnapshotIDGenerator) ValidateSnapshotID(snapshotID string) bool {
	// Format: 16 hex chars
	if len(snapshotID) != 16 {
		return false
	}
	// Check if all chars are valid hex
	for i := 0; i < 16; i++ {
		c := snapshotID[i]
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')) {
			return false
		}
	}
	return true
}
