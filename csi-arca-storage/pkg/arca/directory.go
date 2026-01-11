package arca

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
)

// CreateDirectory creates a directory with optional quota (idempotent)
func (c *Client) CreateDirectory(ctx context.Context, req *CreateDirectoryRequest) error {
	_, err := c.doRequest(ctx, http.MethodPost, "/v1/directories", req)
	if err != nil {
		if err == ErrDirectoryAlreadyExists {
			return nil // Idempotent
		}
		return err
	}
	return nil
}

// DeleteDirectory deletes a directory (idempotent)
func (c *Client) DeleteDirectory(ctx context.Context, svmName, path string) error {
	params := url.Values{}
	params.Set("path", path)

	_, err := c.doRequest(ctx, http.MethodDelete, fmt.Sprintf("/v1/directories/%s", svmName), nil, params)
	if err != nil {
		if err == ErrDirectoryNotFound {
			return nil // Idempotent
		}
		return err
	}
	return nil
}
