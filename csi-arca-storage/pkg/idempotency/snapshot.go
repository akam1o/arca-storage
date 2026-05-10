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

// GenerateSnapshotID creates a deterministic snapshot ID from request name.
// Format: {hash(name)[:32]} (128-bit hash, NO "snap-" prefix here)
// The "snap-" prefix is added when constructing the full path
func (g *SnapshotIDGenerator) GenerateSnapshotID(name string) string {
	h := sha256.Sum256([]byte(name))
	return hex.EncodeToString(h[:16])
}

// ValidateSnapshotID checks if a snapshot ID has the correct format
func (g *SnapshotIDGenerator) ValidateSnapshotID(snapshotID string) bool {
	// Accept legacy 64-bit IDs and new 128-bit IDs.
	if len(snapshotID) != 16 && len(snapshotID) != 32 {
		return false
	}
	// Check if all chars are valid hex
	for i := 0; i < len(snapshotID); i++ {
		if !isLowerHex(snapshotID[i]) {
			return false
		}
	}
	return true
}
