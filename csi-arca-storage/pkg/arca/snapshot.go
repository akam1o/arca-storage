package arca

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
)

// CreateSnapshot creates a snapshot via ARCA API (server-side reflink, idempotent)
func (c *Client) CreateSnapshot(ctx context.Context, req *CreateSnapshotRequest) error {
	_, err := c.doRequest(ctx, http.MethodPost, "/v1/snapshots", req)
	if err != nil {
		if err == ErrSnapshotAlreadyExists {
			return nil // Idempotent
		}
		return err
	}
	return nil
}

// DeleteSnapshot deletes a snapshot via ARCA API (idempotent)
func (c *Client) DeleteSnapshot(ctx context.Context, svmName, snapshotPath string) error {
	params := url.Values{}
	params.Set("path", snapshotPath)

	_, err := c.doRequest(ctx, http.MethodDelete, fmt.Sprintf("/v1/snapshots/%s", svmName), nil, params)
	if err != nil {
		if err == ErrSnapshotNotFound {
			return nil // Idempotent
		}
		return err
	}
	return nil
}

// RestoreSnapshot restores a volume from snapshot (reflink clone)
func (c *Client) RestoreSnapshot(ctx context.Context, req *RestoreSnapshotRequest) error {
	_, err := c.doRequest(ctx, http.MethodPost, "/v1/snapshots/restore", req)
	return err
}
