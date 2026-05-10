package config

import (
	"strings"
	"testing"
)

func minimalConfigWithoutPools() *Config {
	return &Config{
		ARCA: ArcaConfig{
			BaseURL:   "https://arca-api.example.com",
			AuthToken: "test-token",
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

func TestValidateForModeRequiresTokenByDefault(t *testing.T) {
	cfg := minimalConfigWithoutPools()
	cfg.ARCA.AuthToken = ""

	err := cfg.ValidateForMode("node")
	if err == nil {
		t.Fatal("ValidateForMode(node) error = nil, want auth token error")
	}
	if !strings.Contains(err.Error(), "arca.auth_token is required") {
		t.Fatalf("ValidateForMode(node) error = %v, want auth token error", err)
	}
}

func TestValidateForModeAllowsExplicitNoAuth(t *testing.T) {
	cfg := minimalConfigWithoutPools()
	cfg.ARCA.AuthType = AuthTypeNone
	cfg.ARCA.AuthToken = ""

	if err := cfg.ValidateForMode("node"); err != nil {
		t.Fatalf("ValidateForMode(node) error = %v", err)
	}
}

func TestValidateForModeRejectsInvalidAuthType(t *testing.T) {
	cfg := minimalConfigWithoutPools()
	cfg.ARCA.AuthType = "basic"

	err := cfg.ValidateForMode("node")
	if err == nil {
		t.Fatal("ValidateForMode(node) error = nil, want auth type error")
	}
	if !strings.Contains(err.Error(), "arca.auth_type") {
		t.Fatalf("ValidateForMode(node) error = %v, want auth type error", err)
	}
}

func TestToArcaClientConfigOmitsTokenWhenAuthDisabled(t *testing.T) {
	cfg := minimalConfigWithoutPools()
	cfg.ARCA.AuthType = AuthTypeNone

	clientConfig := cfg.ToArcaClientConfig()
	if clientConfig.AuthToken != "" {
		t.Fatalf("AuthToken = %q, want empty token for auth_type none", clientConfig.AuthToken)
	}
}
