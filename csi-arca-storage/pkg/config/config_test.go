package config

import (
	"os"
	"path/filepath"
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

func TestValidateForModeRejectsUnsafeNodeFilesystemPaths(t *testing.T) {
	tests := []struct {
		name    string
		mutate  func(*Config)
		wantErr string
	}{
		{
			name: "relative state file path",
			mutate: func(cfg *Config) {
				cfg.Driver.StateFilePath = "state/node.json"
			},
			wantErr: "driver.state_file_path must be an absolute path",
		},
		{
			name: "unclean state file path",
			mutate: func(cfg *Config) {
				cfg.Driver.StateFilePath = "/var/lib/csi-arca-storage/../node.json"
			},
			wantErr: "driver.state_file_path must be canonical",
		},
		{
			name: "relative base mount path",
			mutate: func(cfg *Config) {
				cfg.Driver.BaseMountPath = "mounts"
			},
			wantErr: "driver.base_mount_path must be an absolute path",
		},
		{
			name: "root base mount path",
			mutate: func(cfg *Config) {
				cfg.Driver.BaseMountPath = "/"
			},
			wantErr: "driver.base_mount_path must not be the filesystem root",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfg := minimalConfigWithoutPools()
			tt.mutate(cfg)

			err := cfg.ValidateForMode("node")
			if err == nil {
				t.Fatal("ValidateForMode(node) error = nil, want path validation error")
			}
			if !strings.Contains(err.Error(), tt.wantErr) {
				t.Fatalf("ValidateForMode(node) error = %v, want %q", err, tt.wantErr)
			}
		})
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

func TestValidateForModeRejectsBlankAuthToken(t *testing.T) {
	cfg := minimalConfigWithoutPools()
	cfg.ARCA.AuthToken = " \t\n "

	err := cfg.ValidateForMode("node")
	if err == nil {
		t.Fatal("ValidateForMode(node) error = nil, want blank auth token error")
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

func TestValidateForModeRejectsRemoteHTTPTokenWithoutOptIn(t *testing.T) {
	cfg := minimalConfigWithoutPools()
	cfg.ARCA.BaseURL = "http://192.0.2.10:8080"

	err := cfg.ValidateForMode("node")
	if err == nil {
		t.Fatal("ValidateForMode(node) error = nil, want remote HTTP token transport error")
	}
	if !strings.Contains(err.Error(), "remote plain HTTP") {
		t.Fatalf("ValidateForMode(node) error = %v, want remote plain HTTP error", err)
	}
}

func TestValidateForModeAllowsRemoteHTTPTokenWithExplicitOptIn(t *testing.T) {
	cfg := minimalConfigWithoutPools()
	cfg.ARCA.BaseURL = "http://192.0.2.10:8080"
	cfg.ARCA.AllowInsecureTokenTransport = true

	if err := cfg.ValidateForMode("node"); err != nil {
		t.Fatalf("ValidateForMode(node) error = %v", err)
	}
}

func TestValidateForModeRejectsPartialClientCertificatePair(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*Config)
	}{
		{
			name: "cert without key",
			mutate: func(cfg *Config) {
				cfg.ARCA.TLS.ClientCertPath = "/etc/csi-arca-storage/tls/client.crt"
			},
		},
		{
			name: "key without cert",
			mutate: func(cfg *Config) {
				cfg.ARCA.TLS.ClientKeyPath = "/etc/csi-arca-storage/tls/client.key"
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfg := minimalConfigWithoutPools()
			tt.mutate(cfg)

			err := cfg.ValidateForMode("node")
			if err == nil {
				t.Fatal("ValidateForMode(node) error = nil, want TLS pair validation error")
			}
			if !strings.Contains(err.Error(), "arca.tls.client_cert_path and arca.tls.client_key_path") {
				t.Fatalf("ValidateForMode(node) error = %v, want TLS pair validation error", err)
			}
		})
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

func TestToArcaClientConfigTrimsAuthToken(t *testing.T) {
	cfg := minimalConfigWithoutPools()
	cfg.ARCA.AuthToken = " test-token \n"

	clientConfig := cfg.ToArcaClientConfig()
	if clientConfig.AuthToken != "test-token" {
		t.Fatalf("AuthToken = %q, want trimmed token", clientConfig.AuthToken)
	}
}

func TestToArcaClientConfigPropagatesInsecureTokenTransportOptIn(t *testing.T) {
	cfg := minimalConfigWithoutPools()
	cfg.ARCA.AllowInsecureTokenTransport = true

	clientConfig := cfg.ToArcaClientConfig()
	if !clientConfig.AllowInsecureTokenTransport {
		t.Fatal("AllowInsecureTokenTransport = false, want true")
	}
}

func TestLoadConfigTrimsAuthTokenAndIgnoresBlankEnvOverride(t *testing.T) {
	configPath := writeConfigFile(t, `
arca:
  base_url: "https://arca-api.example.com"
  auth_token: " file-token "
driver:
  endpoint: "unix:///csi/csi.sock"
`)

	t.Setenv("ARCA_AUTH_TOKEN", " \t\n ")

	cfg, err := LoadConfig(configPath)
	if err != nil {
		t.Fatalf("LoadConfig() error = %v", err)
	}
	if cfg.ARCA.AuthToken != "file-token" {
		t.Fatalf("AuthToken = %q, want file token preserved and trimmed", cfg.ARCA.AuthToken)
	}
}

func TestLoadConfigTrimsEnvAuthTokenOverride(t *testing.T) {
	configPath := writeConfigFile(t, `
arca:
  base_url: "https://arca-api.example.com"
  auth_token: "file-token"
driver:
  endpoint: "unix:///csi/csi.sock"
`)

	t.Setenv("ARCA_AUTH_TOKEN", " env-token \n")

	cfg, err := LoadConfig(configPath)
	if err != nil {
		t.Fatalf("LoadConfig() error = %v", err)
	}
	if cfg.ARCA.AuthToken != "env-token" {
		t.Fatalf("AuthToken = %q, want trimmed env token", cfg.ARCA.AuthToken)
	}
}

func writeConfigFile(t *testing.T, content string) string {
	t.Helper()

	configPath := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(configPath, []byte(content), 0o600); err != nil {
		t.Fatalf("failed to write test config: %v", err)
	}
	return configPath
}
