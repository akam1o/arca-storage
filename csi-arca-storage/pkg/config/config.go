package config

import (
	"fmt"
	"os"
	"path/filepath"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/akam1o/csi-arca-storage/pkg/arca"
)

const (
	AuthTypeToken = "token"
	AuthTypeNone  = "none"
)

// Config represents the CSI driver configuration
type Config struct {
	// ARCA API configuration
	ARCA ArcaConfig `yaml:"arca"`

	// Network configuration
	Network NetworkConfig `yaml:"network"`

	// Driver configuration
	Driver DriverConfig `yaml:"driver"`
}

// ArcaConfig holds ARCA API configuration
type ArcaConfig struct {
	BaseURL   string    `yaml:"base_url"`
	Timeout   Duration  `yaml:"timeout"`
	AuthType  string    `yaml:"auth_type"`
	AuthToken string    `yaml:"auth_token"`
	TLS       TLSConfig `yaml:"tls"`
}

// TLSConfig holds TLS configuration
type TLSConfig struct {
	CACertPath     string `yaml:"ca_cert_path"`
	ClientCertPath string `yaml:"client_cert_path"`
	ClientKeyPath  string `yaml:"client_key_path"`
	InsecureSkip   bool   `yaml:"insecure_skip_verify"`
}

// NetworkConfig holds network configuration
type NetworkConfig struct {
	Pools []PoolConfig `yaml:"pools"`
	MTU   int          `yaml:"mtu"`
}

// PoolConfig represents an IP pool configuration
type PoolConfig struct {
	CIDR    string `yaml:"cidr"`
	Range   string `yaml:"range"`
	VLANID  int    `yaml:"vlan"`
	Gateway string `yaml:"gateway"`
}

// DriverConfig holds driver-specific configuration
type DriverConfig struct {
	NodeID        string `yaml:"node_id"`
	Endpoint      string `yaml:"endpoint"`
	StateFilePath string `yaml:"state_file_path"`
	BaseMountPath string `yaml:"base_mount_path"`
}

// Duration is a wrapper for time.Duration to support YAML unmarshaling
type Duration struct {
	time.Duration
}

func (d *Duration) UnmarshalYAML(node *yaml.Node) error {
	var s string
	if err := node.Decode(&s); err != nil {
		return err
	}
	duration, err := time.ParseDuration(s)
	if err != nil {
		return err
	}
	d.Duration = duration
	return nil
}

func (d Duration) MarshalYAML() (interface{}, error) {
	return d.String(), nil
}

// LoadConfig loads configuration from a file
func LoadConfig(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("failed to read config file: %w", err)
	}

	var config Config
	if err := yaml.Unmarshal(data, &config); err != nil {
		return nil, fmt.Errorf("failed to parse config: %w", err)
	}

	// Set defaults
	if config.ARCA.Timeout.Duration == 0 {
		config.ARCA.Timeout.Duration = 30 * time.Second
	}
	if config.Network.MTU == 0 {
		config.Network.MTU = 1500
	}
	if config.ARCA.AuthType == "" {
		config.ARCA.AuthType = AuthTypeToken
	}

	// Override auth token from environment if set
	if envToken := os.Getenv("ARCA_AUTH_TOKEN"); envToken != "" {
		config.ARCA.AuthToken = envToken
	}

	return &config, nil
}

// Validate validates the configuration for controller mode.
func (c *Config) Validate() error {
	return c.ValidateForMode("controller")
}

// ValidateForMode validates the configuration required by the requested driver mode.
func (c *Config) ValidateForMode(mode string) error {
	if c.ARCA.BaseURL == "" {
		return fmt.Errorf("arca.base_url is required")
	}
	authType := normalizedAuthType(c.ARCA.AuthType)
	switch authType {
	case AuthTypeToken:
		if c.ARCA.AuthToken == "" {
			return fmt.Errorf("arca.auth_token is required when arca.auth_type is %q", AuthTypeToken)
		}
	case AuthTypeNone:
	default:
		return fmt.Errorf("arca.auth_type must be %q or %q", AuthTypeToken, AuthTypeNone)
	}
	if err := c.validateTLSConfig(); err != nil {
		return err
	}

	switch mode {
	case "controller":
		if err := c.validateNetworkPools(); err != nil {
			return err
		}
	case "node":
		if err := c.validateNodeFilesystemPaths(); err != nil {
			return err
		}
	default:
		return fmt.Errorf("invalid driver mode %q", mode)
	}

	if c.Driver.Endpoint == "" {
		return fmt.Errorf("driver.endpoint is required")
	}

	return nil
}

func (c *Config) validateTLSConfig() error {
	hasClientCert := c.ARCA.TLS.ClientCertPath != ""
	hasClientKey := c.ARCA.TLS.ClientKeyPath != ""
	if hasClientCert != hasClientKey {
		return fmt.Errorf("arca.tls.client_cert_path and arca.tls.client_key_path must be set together")
	}
	return nil
}

func normalizedAuthType(authType string) string {
	if authType == "" {
		return AuthTypeToken
	}
	return authType
}

func validateOptionalAbsolutePath(value, field string) error {
	if value == "" {
		return nil
	}
	if !filepath.IsAbs(value) {
		return fmt.Errorf("%s must be an absolute path", field)
	}
	cleaned := filepath.Clean(value)
	if cleaned != value {
		return fmt.Errorf("%s must be canonical", field)
	}
	if cleaned == string(filepath.Separator) {
		return fmt.Errorf("%s must not be the filesystem root", field)
	}
	return nil
}

func (c *Config) validateNodeFilesystemPaths() error {
	if err := validateOptionalAbsolutePath(c.Driver.StateFilePath, "driver.state_file_path"); err != nil {
		return err
	}
	if err := validateOptionalAbsolutePath(c.Driver.BaseMountPath, "driver.base_mount_path"); err != nil {
		return err
	}
	return nil
}

func (c *Config) validateNetworkPools() error {
	if len(c.Network.Pools) == 0 {
		return fmt.Errorf("at least one network pool is required")
	}

	for i, pool := range c.Network.Pools {
		if pool.CIDR == "" {
			return fmt.Errorf("network.pools[%d].cidr is required", i)
		}
	}

	return nil
}

// ToArcaClientConfig converts to ARCA client configuration
func (c *Config) ToArcaClientConfig() *arca.ClientConfig {
	authToken := c.ARCA.AuthToken
	if normalizedAuthType(c.ARCA.AuthType) == AuthTypeNone {
		authToken = ""
	}
	return &arca.ClientConfig{
		BaseURL:    c.ARCA.BaseURL,
		Timeout:    c.ARCA.Timeout.Duration,
		RetryCount: 3,
		AuthToken:  authToken,
		TLSConfig: &arca.TLSConfig{
			CACertPath:     c.ARCA.TLS.CACertPath,
			ClientCertPath: c.ARCA.TLS.ClientCertPath,
			ClientKeyPath:  c.ARCA.TLS.ClientKeyPath,
			InsecureSkip:   c.ARCA.TLS.InsecureSkip,
		},
	}
}

// ToArcaPoolConfigs converts to ARCA pool configurations
func (c *Config) ToArcaPoolConfigs() []arca.PoolConfig {
	pools := make([]arca.PoolConfig, len(c.Network.Pools))
	for i, p := range c.Network.Pools {
		pools[i] = arca.PoolConfig{
			CIDR:    p.CIDR,
			Range:   p.Range,
			VLANID:  p.VLANID,
			Gateway: p.Gateway,
		}
	}
	return pools
}
