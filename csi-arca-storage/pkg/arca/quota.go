package arca

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
)

// SetQuota sets XFS project quota for a directory
func (c *Client) SetQuota(ctx context.Context, req *SetQuotaRequest) error {
	_, err := c.doRequest(ctx, http.MethodPost, "/v1/quotas", req)
	return err
}

// GetQuota gets current quota usage for a path
func (c *Client) GetQuota(ctx context.Context, svmName, path string) (*QuotaInfo, error) {
	params := url.Values{}
	params.Set("path", path)

	respBody, err := c.doRequest(ctx, http.MethodGet, fmt.Sprintf("/v1/quotas/%s", svmName), nil, params)
	if err != nil {
		return nil, err
	}

	var response struct {
		Data QuotaInfo `json:"data"`
	}
	if err := json.Unmarshal(respBody, &response); err != nil {
		return nil, fmt.Errorf("failed to unmarshal response: %w", err)
	}

	return &response.Data, nil
}

// ExpandQuota expands existing quota
func (c *Client) ExpandQuota(ctx context.Context, req *ExpandQuotaRequest) error {
	_, err := c.doRequest(ctx, http.MethodPatch, "/v1/quotas", req)
	return err
}
