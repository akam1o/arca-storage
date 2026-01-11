package arca

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"time"

	"k8s.io/klog/v2"
)

// Client is an ARCA REST API client
type Client struct {
	baseURL    string
	httpClient *http.Client
	timeout    time.Duration
	retryCount int
	authToken  string
}

// ClientConfig holds configuration for the ARCA client
type ClientConfig struct {
	BaseURL    string
	Timeout    time.Duration
	RetryCount int
	AuthToken  string
	TLSConfig  *TLSConfig
}

// TLSConfig holds TLS configuration
type TLSConfig struct {
	CACertPath     string
	ClientCertPath string
	ClientKeyPath  string
	InsecureSkip   bool
}

// NewClient creates a new ARCA API client
func NewClient(config *ClientConfig) (*Client, error) {
	if config.Timeout == 0 {
		config.Timeout = 30 * time.Second
	}
	if config.RetryCount == 0 {
		config.RetryCount = 3
	}

	httpClient := &http.Client{
		Timeout: config.Timeout,
	}

	// Configure TLS if provided
	if config.TLSConfig != nil {
		tlsConfig, err := buildTLSConfig(config.TLSConfig)
		if err != nil {
			return nil, fmt.Errorf("failed to build TLS config: %w", err)
		}
		httpClient.Transport = &http.Transport{
			TLSClientConfig: tlsConfig,
		}
	}

	return &Client{
		baseURL:    config.BaseURL,
		httpClient: httpClient,
		timeout:    config.Timeout,
		retryCount: config.RetryCount,
		authToken:  config.AuthToken,
	}, nil
}

// buildTLSConfig builds TLS configuration from file paths
func buildTLSConfig(config *TLSConfig) (*tls.Config, error) {
	tlsConfig := &tls.Config{
		InsecureSkipVerify: config.InsecureSkip,
	}

	// Load CA certificate
	if config.CACertPath != "" {
		caCert, err := os.ReadFile(config.CACertPath)
		if err != nil {
			return nil, fmt.Errorf("failed to read CA cert: %w", err)
		}
		caCertPool := x509.NewCertPool()
		if !caCertPool.AppendCertsFromPEM(caCert) {
			return nil, fmt.Errorf("failed to parse CA cert")
		}
		tlsConfig.RootCAs = caCertPool
	}

	// Load client certificate and key
	if config.ClientCertPath != "" && config.ClientKeyPath != "" {
		cert, err := tls.LoadX509KeyPair(config.ClientCertPath, config.ClientKeyPath)
		if err != nil {
			return nil, fmt.Errorf("failed to load client cert/key: %w", err)
		}
		tlsConfig.Certificates = []tls.Certificate{cert}
	}

	return tlsConfig, nil
}

// doRequest performs HTTP request with exponential backoff retry
func (c *Client) doRequest(ctx context.Context, method, path string, body interface{}, queryParams ...url.Values) ([]byte, error) {
	var lastErr error

	for attempt := 0; attempt <= c.retryCount; attempt++ {
		if attempt > 0 {
			backoff := time.Duration(1<<uint(attempt-1)) * time.Second
			klog.V(4).Infof("Retrying request (attempt %d/%d) after %v", attempt+1, c.retryCount+1, backoff)
			select {
			case <-time.After(backoff):
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}

		resp, err := c.doRequestOnce(ctx, method, path, body, queryParams...)
		if err == nil {
			return resp, nil
		}

		lastErr = err

		// Don't retry on certain errors
		if isNonRetryableError(err) {
			klog.V(4).Infof("Non-retryable error: %v", err)
			break
		}

		klog.V(4).Infof("Request failed (attempt %d/%d): %v", attempt+1, c.retryCount+1, err)
	}

	return nil, fmt.Errorf("request failed after %d attempts: %w", c.retryCount+1, lastErr)
}

// doRequestOnce performs a single HTTP request
func (c *Client) doRequestOnce(ctx context.Context, method, path string, body interface{}, queryParams ...url.Values) ([]byte, error) {
	// Build URL
	reqURL := c.baseURL + path
	if len(queryParams) > 0 && queryParams[0] != nil {
		reqURL += "?" + queryParams[0].Encode()
	}

	// Marshal body
	var bodyReader io.Reader
	if body != nil {
		bodyBytes, err := json.Marshal(body)
		if err != nil {
			return nil, fmt.Errorf("failed to marshal request body: %w", err)
		}
		bodyReader = bytes.NewReader(bodyBytes)
	}

	// Create request
	req, err := http.NewRequestWithContext(ctx, method, reqURL, bodyReader)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	// Set headers
	req.Header.Set("Content-Type", "application/json")
	if c.authToken != "" {
		req.Header.Set("Authorization", "Bearer "+c.authToken)
	}

	// Execute request
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("http request failed: %w", err)
	}
	defer resp.Body.Close()

	// Read response body
	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}

	// Check status code
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		// Try to parse error message from response
		var apiResp APIResponse
		if err := json.Unmarshal(respBody, &apiResp); err == nil && apiResp.Error != "" {
			return nil, MapHTTPStatusToError(resp.StatusCode, apiResp.Error)
		}
		return nil, MapHTTPStatusToError(resp.StatusCode, string(respBody))
	}

	return respBody, nil
}

// isNonRetryableError checks if an error should not be retried
func isNonRetryableError(err error) bool {
	// Don't retry on 4xx errors except 408 (timeout) and 429 (rate limit)
	if apiErr, ok := err.(*APIError); ok {
		if apiErr.StatusCode >= 400 && apiErr.StatusCode < 500 {
			return apiErr.StatusCode != 408 && apiErr.StatusCode != 429
		}
	}

	// Don't retry on specific known errors
	switch err {
	case ErrSVMAlreadyExists, ErrDirectoryAlreadyExists, ErrSnapshotAlreadyExists:
		return true
	case ErrSVMNotFound, ErrDirectoryNotFound, ErrSnapshotNotFound, ErrQuotaNotFound:
		return true
	}

	return false
}

// GetSVM retrieves SVM information
func (c *Client) GetSVM(ctx context.Context, name string) (*SVM, error) {
	respBody, err := c.doRequest(ctx, http.MethodGet, fmt.Sprintf("/v1/svms/%s", name), nil)
	if err != nil {
		return nil, err
	}

	var response struct {
		Data SVM `json:"data"`
	}
	if err := json.Unmarshal(respBody, &response); err != nil {
		return nil, fmt.Errorf("failed to unmarshal response: %w", err)
	}

	return &response.Data, nil
}

// CreateSVM creates a new SVM (idempotent)
func (c *Client) CreateSVM(ctx context.Context, req *CreateSVMRequest) (*SVM, error) {
	respBody, err := c.doRequest(ctx, http.MethodPost, "/v1/svms", req)
	if err != nil {
		// If SVM already exists, try to get it
		if err == ErrSVMAlreadyExists {
			return c.GetSVM(ctx, req.Name)
		}
		return nil, err
	}

	var response struct {
		Data SVM `json:"data"`
	}
	if err := json.Unmarshal(respBody, &response); err != nil {
		return nil, fmt.Errorf("failed to unmarshal response: %w", err)
	}

	return &response.Data, nil
}

// DeleteSVM deletes an SVM (idempotent)
func (c *Client) DeleteSVM(ctx context.Context, name string) error {
	_, err := c.doRequest(ctx, http.MethodDelete, fmt.Sprintf("/v1/svms/%s", name), nil)
	if err != nil {
		if err == ErrSVMNotFound {
			return nil // Idempotent
		}
		return err
	}
	return nil
}

// ListSVMs lists all SVMs
func (c *Client) ListSVMs(ctx context.Context) ([]SVM, error) {
	respBody, err := c.doRequest(ctx, http.MethodGet, "/v1/svms", nil)
	if err != nil {
		return nil, err
	}

	var response struct {
		Data []SVM `json:"data"`
	}
	if err := json.Unmarshal(respBody, &response); err != nil {
		return nil, fmt.Errorf("failed to unmarshal response: %w", err)
	}

	return response.Data, nil
}

// GetSVMCapacity retrieves SVM capacity information
func (c *Client) GetSVMCapacity(ctx context.Context, svmName string) (*CapacityInfo, error) {
	respBody, err := c.doRequest(ctx, http.MethodGet, fmt.Sprintf("/v1/svms/%s/capacity", svmName), nil)
	if err != nil {
		return nil, err
	}

	var response struct {
		Data CapacityInfo `json:"data"`
	}
	if err := json.Unmarshal(respBody, &response); err != nil {
		return nil, fmt.Errorf("failed to unmarshal response: %w", err)
	}

	return &response.Data, nil
}
