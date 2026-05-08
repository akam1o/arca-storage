package config

import (
	"strings"
	"testing"
)

func minimalConfigWithoutPools() *Config {
	return &Config{
		ARCA: ArcaConfig{
			BaseURL: "https://arca-api.example.com",
		},
		Driver: DriverConfig{
			Endpoint: "unix:///csi/csi.sock",
		},
	}
}

func TestValidateForModeAllowsNodeWithoutNetworkPools(t *testing.T) {
	cfg := minimalConfigWithoutPools()

	if err := cfg.ValidateForMode("node"); err != nil {
		t.Fatalf("ValidateForMode(node) error = %v", err)
	}
}

func TestValidateForModeRequiresControllerNetworkPools(t *testing.T) {
	cfg := minimalConfigWithoutPools()

	err := cfg.ValidateForMode("controller")
	if err == nil {
		t.Fatal("ValidateForMode(controller) error = nil, want network pool error")
	}
	if !strings.Contains(err.Error(), "at least one network pool is required") {
		t.Fatalf("ValidateForMode(controller) error = %v, want network pool error", err)
	}
}

func TestValidatePreservesControllerValidation(t *testing.T) {
	cfg := minimalConfigWithoutPools()

	err := cfg.Validate()
	if err == nil {
		t.Fatal("Validate() error = nil, want network pool error")
	}
	if !strings.Contains(err.Error(), "at least one network pool is required") {
		t.Fatalf("Validate() error = %v, want network pool error", err)
	}
}

func TestValidateForModeRejectsInvalidMode(t *testing.T) {
	cfg := minimalConfigWithoutPools()

	err := cfg.ValidateForMode("all")
	if err == nil {
		t.Fatal("ValidateForMode(all) error = nil, want invalid mode error")
	}
	if !strings.Contains(err.Error(), "invalid driver mode") {
		t.Fatalf("ValidateForMode(all) error = %v, want invalid mode error", err)
	}
}
