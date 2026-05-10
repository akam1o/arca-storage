package idempotency

import "testing"

func TestGenerateVolumeIDUses128BitDigest(t *testing.T) {
	gen := NewVolumeIDGenerator()

	got := gen.GenerateVolumeID("pvc-a")
	const want = "pvc-38f2b742b935d124a05585a67175089f"

	if got != want {
		t.Fatalf("GenerateVolumeID() = %q, want %q", got, want)
	}
	if !gen.ValidateVolumeID(got) {
		t.Fatalf("ValidateVolumeID(%q) = false, want true", got)
	}
}

func TestValidateVolumeIDAcceptsLegacyAndCurrentFormats(t *testing.T) {
	gen := NewVolumeIDGenerator()
	tests := []struct {
		name     string
		volumeID string
		want     bool
	}{
		{"legacy64", "pvc-0123456789abcdef", true},
		{"current128", "pvc-0123456789abcdef0123456789abcdef", true},
		{"missingPrefix", "vol-0123456789abcdef0123456789abcdef", false},
		{"tooShort", "pvc-0123456789abcde", false},
		{"tooLong", "pvc-0123456789abcdef0123456789abcdef00", false},
		{"uppercase", "pvc-0123456789ABCDEF0123456789abcdef", false},
		{"notHex", "pvc-0123456789abcdef0123456789abcdeg", false},
		{"empty", "", false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := gen.ValidateVolumeID(tt.volumeID); got != tt.want {
				t.Fatalf("ValidateVolumeID(%q) = %v, want %v", tt.volumeID, got, tt.want)
			}
		})
	}
}

func TestGenerateSnapshotIDUses128BitDigest(t *testing.T) {
	gen := NewSnapshotIDGenerator()

	got := gen.GenerateSnapshotID("source-vol/snap-a")
	const want = "7600fa5448cb799c0837b12ebaded407"

	if got != want {
		t.Fatalf("GenerateSnapshotID() = %q, want %q", got, want)
	}
	if !gen.ValidateSnapshotID(got) {
		t.Fatalf("ValidateSnapshotID(%q) = false, want true", got)
	}
}

func TestValidateSnapshotIDAcceptsLegacyAndCurrentFormats(t *testing.T) {
	gen := NewSnapshotIDGenerator()
	tests := []struct {
		name       string
		snapshotID string
		want       bool
	}{
		{"legacy64", "0123456789abcdef", true},
		{"current128", "0123456789abcdef0123456789abcdef", true},
		{"tooShort", "0123456789abcde", false},
		{"tooLong", "0123456789abcdef0123456789abcdef00", false},
		{"uppercase", "0123456789ABCDEF0123456789abcdef", false},
		{"notHex", "0123456789abcdef0123456789abcdeg", false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := gen.ValidateSnapshotID(tt.snapshotID); got != tt.want {
				t.Fatalf("ValidateSnapshotID(%q) = %v, want %v", tt.snapshotID, got, tt.want)
			}
		})
	}
}
