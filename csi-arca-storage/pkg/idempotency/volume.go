package idempotency

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
)

// VolumeIDGenerator generates stable volume IDs from PVC names
type VolumeIDGenerator struct{}

// NewVolumeIDGenerator creates a new volume ID generator
func NewVolumeIDGenerator() *VolumeIDGenerator {
	return &VolumeIDGenerator{}
}

// GenerateVolumeID creates a deterministic volume ID from request name
// Format: pvc-{hash(name)[:16]} (64-bit hash to reduce collision risk)
func (g *VolumeIDGenerator) GenerateVolumeID(name string) string {
	h := sha256.Sum256([]byte(name))
	return fmt.Sprintf("pvc-%s", hex.EncodeToString(h[:8]))
}

// ValidateVolumeID checks if a volume ID has the correct format
func (g *VolumeIDGenerator) ValidateVolumeID(volumeID string) bool {
	// Format: pvc-{16 hex chars}
	if len(volumeID) != 20 { // "pvc-" (4) + 16 hex chars
		return false
	}
	if volumeID[:4] != "pvc-" {
		return false
	}
	// Check if remaining chars are valid hex
	for i := 4; i < 20; i++ {
		c := volumeID[i]
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')) {
			return false
		}
	}
	return true
}
