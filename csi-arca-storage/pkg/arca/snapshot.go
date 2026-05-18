package arca

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
)

// CreateSnapshot creates a snapshot via ARCA API (server-side reflink).
func (c *Client) CreateSnapshot(ctx context.Context, req *CreateSnapshotRequest) error {
	_, err := c.doRequest(ctx, http.MethodPost, "/v1/snapshots", req)
	return err
}

// ListSnapshots lists snapshots, optionally filtered by SVM, volume, and snapshot name.
func (c *Client) ListSnapshots(ctx context.Context, svmName, volume, name string) ([]Snapshot, error) {
	var all []Snapshot
	cursor := ""
	seenCursors := map[string]struct{}{}

	for {
		params := url.Values{}
		if svmName != "" {
			params.Set("svm", svmName)
		}
		if volume != "" {
			params.Set("volume", volume)
		}
		if name != "" {
			params.Set("name", name)
		}
		if cursor != "" {
			params.Set("cursor", cursor)
		}

		resp, err := c.doRequest(ctx, http.MethodGet, "/v1/snapshots", nil, params)
		if err != nil {
			return nil, err
		}

		var listResp struct {
			Data struct {
				Items      []Snapshot `json:"items"`
				NextCursor *string    `json:"next_cursor"`
			} `json:"data"`
		}
		if err := json.Unmarshal(resp, &listResp); err != nil {
			return nil, fmt.Errorf("failed to parse snapshot list response: %w", err)
		}

		all = append(all, listResp.Data.Items...)
		nextCursor := ""
		if listResp.Data.NextCursor != nil {
			nextCursor = *listResp.Data.NextCursor
		}
		if nextCursor == "" {
			return all, nil
		}
		if _, ok := seenCursors[nextCursor]; ok {
			return nil, fmt.Errorf("%w: repeated snapshot pagination cursor %q", ErrInvalidResponse, nextCursor)
		}
		seenCursors[nextCursor] = struct{}{}
		cursor = nextCursor
	}
}

// DeleteSnapshot deletes a snapshot via ARCA API (idempotent)
func (c *Client) DeleteSnapshot(ctx context.Context, name, svmName, volume string) error {
	params := url.Values{}
	params.Set("svm", svmName)
	params.Set("volume", volume)

	_, err := c.doRequest(ctx, http.MethodDelete, fmt.Sprintf("/v1/snapshots/%s", pathSegment(name)), nil, params)
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
	sourceVolume := req.SourceVolume
	if sourceVolume == "" {
		sourceVolume = req.Name
	}

	_, err := c.doRequest(ctx, http.MethodPost, fmt.Sprintf("/v1/volumes/%s/clone", pathSegment(sourceVolume)), req)
	return err
}

// RestoreSnapshot restores a volume from snapshot (reflink clone)
func (c *Client) RestoreSnapshot(ctx context.Context, req *RestoreSnapshotRequest) error {
	if req == nil {
		return fmt.Errorf("restore snapshot request is required")
	}
	if req.SourceVolume == "" {
		return fmt.Errorf("source volume is required")
	}

	return c.CloneVolumeFromSnapshot(ctx, &CloneVolumeFromSnapshotRequest{
		Name:         req.TargetPath,
		SVM:          req.SVMName,
		SourceVolume: req.SourceVolume,
		Snapshot:     req.SnapshotPath,
	})
}
