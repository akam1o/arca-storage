package arca

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/url"
)

// CreateSnapshot creates a snapshot via ARCA API (server-side reflink, idempotent)
func (c *Client) CreateSnapshot(ctx context.Context, req *CreateSnapshotRequest) error {
	_, err := c.doRequest(ctx, http.MethodPost, "/v1/snapshots", req)
	if err != nil {
		if errors.Is(err, ErrSnapshotAlreadyExists) {
			return nil // Idempotent
		}
		return err
	}
	return nil
}

// DeleteSnapshot deletes a snapshot via ARCA API (idempotent)
func (c *Client) DeleteSnapshot(ctx context.Context, name, svmName, volume string) error {
	params := url.Values{}
	params.Set("svm", svmName)
	params.Set("volume", volume)

	_, err := c.doRequest(ctx, http.MethodDelete, fmt.Sprintf("/v1/snapshots/%s", url.PathEscape(name)), nil, params)
	if err != nil {
		if errors.Is(err, ErrSnapshotNotFound) {
			return nil // Idempotent
		}
		return err
	}
	return nil
}

// CloneVolumeFromSnapshot creates a new volume from a snapshot.
func (c *Client) CloneVolumeFromSnapshot(ctx context.Context, req *CloneVolumeFromSnapshotRequest) error {
	_, err := c.doRequest(ctx, http.MethodPost, fmt.Sprintf("/v1/volumes/%s/clone", url.PathEscape(req.Name)), req)
	if err != nil {
		if IsAlreadyExistsError(err) {
			return nil
		}
		return err
	}
	return nil
}

// RestoreSnapshot restores a volume from snapshot (reflink clone)
func (c *Client) RestoreSnapshot(ctx context.Context, req *RestoreSnapshotRequest) error {
	return c.CloneVolumeFromSnapshot(ctx, &CloneVolumeFromSnapshotRequest{
		Name:     req.TargetPath,
		SVM:      req.SVMName,
		Snapshot: req.SnapshotPath,
	})
}
