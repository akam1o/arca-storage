package arca

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"os"
	"time"

	"k8s.io/klog/v2"
)

const bytesPerGiB = int64(1024 * 1024 * 1024)

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
	defer func() {
		if err := resp.Body.Close(); err != nil {
			klog.Warningf("Failed to close response body: %v", err)
		}
	}()

	// Read response body
	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}

	// Check status code
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		// Try to parse structured error response first
		var errResp ArcaErrorResponse
		if err := json.Unmarshal(respBody, &errResp); err == nil && errResp.Error.Code != "" {
			return nil, MapErrorCodeToError(resp.StatusCode, &errResp.Error)
		}

		// Fall back to legacy text-based error mapping
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
	var apiErr *APIError
	if errors.As(err, &apiErr) {
		if apiErr.StatusCode >= 400 && apiErr.StatusCode < 500 {
			return apiErr.StatusCode != 408 && apiErr.StatusCode != 429
		}
	}

	// Don't retry on specific known errors
	switch {
	case errors.Is(err, ErrSVMAlreadyExists), errors.Is(err, ErrDirectoryAlreadyExists), errors.Is(err, ErrSnapshotAlreadyExists):
		return true
	case errors.Is(err, ErrSVMNotFound), errors.Is(err, ErrDirectoryNotFound), errors.Is(err, ErrSnapshotNotFound), errors.Is(err, ErrQuotaNotFound):
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

	return decodeSVMResponse(respBody)
}

// CreateSVM creates a new SVM (idempotent)
func (c *Client) CreateSVM(ctx context.Context, req *CreateSVMRequest) (*SVM, error) {
	respBody, err := c.doRequest(ctx, http.MethodPost, "/v1/svms", req)
	if err != nil {
		// If SVM already exists, try to get it
		if errors.Is(err, ErrSVMAlreadyExists) {
			return c.GetSVM(ctx, req.Name)
		}
		return nil, err
	}

	return decodeSVMResponse(respBody)
}

// DeleteSVM deletes an SVM (idempotent)
func (c *Client) DeleteSVM(ctx context.Context, name string) error {
	_, err := c.doRequest(ctx, http.MethodDelete, fmt.Sprintf("/v1/svms/%s", name), nil)
	if err != nil {
		if errors.Is(err, ErrSVMNotFound) {
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

	return decodeSVMListResponse(respBody)
}

// GetSVMCapacity retrieves SVM capacity information
func (c *Client) GetSVMCapacity(ctx context.Context, svmName string) (*CapacityInfo, error) {
	respBody, err := c.doRequest(ctx, http.MethodGet, fmt.Sprintf("/v1/svms/%s/capacity", svmName), nil)
	if err != nil {
		return nil, err
	}

	var response struct {
		Data json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(respBody, &response); err != nil {
		return nil, fmt.Errorf("failed to unmarshal response: %w", err)
	}

	if len(response.Data) == 0 {
		return nil, fmt.Errorf("%w: missing data field", ErrInvalidResponse)
	}

	var direct CapacityInfo
	if err := json.Unmarshal(response.Data, &direct); err == nil && !direct.isZero() {
		return &direct, nil
	}

	var nested struct {
		Capacity struct {
			TotalGB float64 `json:"total_gb"`
			FreeGB  float64 `json:"free_gb"`
			UsedGB  float64 `json:"used_gb"`
		} `json:"capacity"`
	}
	if err := json.Unmarshal(response.Data, &nested); err == nil && nested.Capacity.TotalGB > 0 {
		return &CapacityInfo{
			TotalBytes:     gibToBytes(nested.Capacity.TotalGB),
			AvailableBytes: gibToBytes(nested.Capacity.FreeGB),
			UsedBytes:      gibToBytes(nested.Capacity.UsedGB),
		}, nil
	}

	return nil, fmt.Errorf("%w: missing capacity object in response", ErrInvalidResponse)
}

func (c CapacityInfo) isZero() bool {
	return c.TotalBytes == 0 && c.AvailableBytes == 0 && c.UsedBytes == 0
}

func gibToBytes(gib float64) int64 {
	return int64(math.Round(gib * float64(bytesPerGiB)))
}

func decodeSVMResponse(respBody []byte) (*SVM, error) {
	var response struct {
		Data json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(respBody, &response); err != nil {
		return nil, fmt.Errorf("failed to unmarshal response: %w", err)
	}
	if len(response.Data) == 0 {
		return nil, fmt.Errorf("%w: missing data field", ErrInvalidResponse)
	}

	var svm SVM
	if err := json.Unmarshal(response.Data, &svm); err == nil && svm.Name != "" {
		return &svm, nil
	}

	var nested struct {
		SVM SVM `json:"svm"`
	}
	if err := json.Unmarshal(response.Data, &nested); err == nil && nested.SVM.Name != "" {
		return &nested.SVM, nil
	}

	return nil, fmt.Errorf("%w: missing SVM object in response", ErrInvalidResponse)
}

func decodeSVMListResponse(respBody []byte) ([]SVM, error) {
	var response struct {
		Data json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(respBody, &response); err != nil {
		return nil, fmt.Errorf("failed to unmarshal response: %w", err)
	}
	if len(response.Data) == 0 {
		return nil, fmt.Errorf("%w: missing data field", ErrInvalidResponse)
	}

	var svms []SVM
	if err := json.Unmarshal(response.Data, &svms); err == nil {
		return svms, nil
	}

	var nested struct {
		Items []SVM `json:"items"`
		SVMs  []SVM `json:"svms"`
	}
	if err := json.Unmarshal(response.Data, &nested); err == nil {
		if nested.Items != nil {
			return nested.Items, nil
		}
		if nested.SVMs != nil {
			return nested.SVMs, nil
		}
	}

	return nil, fmt.Errorf("%w: missing SVM list in response", ErrInvalidResponse)
}
