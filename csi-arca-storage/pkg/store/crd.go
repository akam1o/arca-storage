// SPDX-License-Identifier: Apache-2.0

package store

import (
	"context"
	"fmt"
	"time"

	"github.com/akam1o/csi-arca-storage/pkg/apis/storage/v1alpha1"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	apiextensionsclientset "k8s.io/apiextensions-apiserver/pkg/client/clientset/clientset"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/util/retry"
	"k8s.io/klog/v2"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	FinalizerArcaStorage = "storage.arca.io/csi-driver"

	crudTimeout = 10 * time.Second
	listTimeout = 30 * time.Second
)

func removeFinalizer(finalizers []string, finalizerToRemove string) []string {
	result := make([]string, 0, len(finalizers))
	for _, f := range finalizers {
		if f != finalizerToRemove {
			result = append(result, f)
		}
	}
	return result
}

func hasFinalizer(finalizers []string, finalizer string) bool {
	for _, f := range finalizers {
		if f == finalizer {
			return true
		}
	}
	return false
}

// CRDStore implements Store interface using Kubernetes Custom Resource Definitions
type CRDStore struct {
	client client.Client
}

// NewCRDStore creates a new CRD-based store using controller-runtime client
func NewCRDStore(config *rest.Config, k8sClient kubernetes.Interface) (*CRDStore, error) {
	// Create runtime scheme and register our types
	scheme := runtime.NewScheme()
	if err := v1alpha1.AddToScheme(scheme); err != nil {
		return nil, fmt.Errorf("failed to add v1alpha1 to scheme: %w", err)
	}
	if err := apiextensionsv1.AddToScheme(scheme); err != nil {
		return nil, fmt.Errorf("failed to add apiextensions to scheme: %w", err)
	}

	// Create controller-runtime client
	c, err := client.New(config, client.Options{Scheme: scheme})
	if err != nil {
		return nil, fmt.Errorf("failed to create controller-runtime client: %w", err)
	}

	// Verify CRDs exist using apiextensions clientset
	ctx, cancel := context.WithTimeout(context.Background(), crudTimeout)
	defer cancel()

	apiextClient, err := apiextensionsclientset.NewForConfig(config)
	if err != nil {
		return nil, fmt.Errorf("failed to create apiextensions client: %w", err)
	}

	requiredCRDs := []string{
		"arcavolumes.storage.arca.io",
		"arcasnapshots.storage.arca.io",
	}

	for _, crdName := range requiredCRDs {
		_, err := apiextClient.ApiextensionsV1().CustomResourceDefinitions().Get(ctx, crdName, metav1.GetOptions{})
		if err != nil {
			return nil, fmt.Errorf("CRD %s not found: %w - Please install CRDs first: kubectl apply -f deploy/crds/", crdName, err)
		}
	}

	klog.Info("All required CRDs are installed")

	return &CRDStore{
		client: c,
	}, nil
}

// CreateVolume stores volume metadata as ArcaVolume CRD (idempotent)
func (s *CRDStore) CreateVolume(ctx context.Context, info *VolumeInfo) error {
	ctx, cancel := context.WithTimeout(ctx, crudTimeout)
	defer cancel()
	if err := ctx.Err(); err != nil {
		return err
	}

	if err := validateVolumeInfo(info); err != nil {
		volumeID := "<nil>"
		if info != nil {
			volumeID = info.VolumeID
		}
		return fmt.Errorf("invalid ArcaVolume metadata for %s: %w", volumeID, err)
	}

	av := volumeInfoToArcaVolume(info)

	err := s.client.Create(ctx, av)
	if err != nil {
		// Map Kubernetes errors to typed store errors
		mapped := MapKubernetesError(err, "ArcaVolume", info.VolumeID)

		// If already exists, this is idempotent - return the mapped error
		// so controller can check parameters
		if IsAlreadyExists(mapped) {
			return mapped
		}

		return fmt.Errorf("failed to create ArcaVolume: %w", mapped)
	}

	klog.Infof("Created ArcaVolume %s", info.VolumeID)
	return nil
}

// UpdateVolume updates existing volume metadata
func (s *CRDStore) UpdateVolume(ctx context.Context, info *VolumeInfo) error {
	ctx, cancel := context.WithTimeout(ctx, crudTimeout)
	defer cancel()
	if err := ctx.Err(); err != nil {
		return err
	}

	if err := validateVolumeInfo(info); err != nil {
		volumeID := "<nil>"
		if info != nil {
			volumeID = info.VolumeID
		}
		return fmt.Errorf("invalid ArcaVolume metadata for %s: %w", volumeID, err)
	}

	err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
		// Get existing resource to preserve metadata and the latest resourceVersion.
		existing := &v1alpha1.ArcaVolume{}
		if err := s.client.Get(ctx, client.ObjectKey{Name: info.VolumeID}, existing); err != nil {
			return err
		}

		// Update spec fields
		updated := volumeInfoToArcaVolume(info)
		if updated.Spec.CapacityBytes < existing.Spec.CapacityBytes {
			updated.Spec.CapacityBytes = existing.Spec.CapacityBytes
		}
		existing.Spec = updated.Spec
		if existing.Labels == nil {
			existing.Labels = make(map[string]string)
		}
		for key, value := range updated.Labels {
			existing.Labels[key] = value
		}
		if updated.Annotations != nil {
			if existing.Annotations == nil {
				existing.Annotations = make(map[string]string)
			}
			for key, value := range updated.Annotations {
				existing.Annotations[key] = value
			}
		}
		if info.TemporaryCloneSnapshot == "" && existing.Annotations != nil {
			delete(existing.Annotations, temporaryCloneSnapshotAnnotation)
		}
		if info.TemporaryCloneSourceVolumePath == "" && existing.Annotations != nil {
			delete(existing.Annotations, temporaryCloneSourceVolumePathAnnotation)
		}

		return s.client.Update(ctx, existing)
	})
	if err != nil {
		return fmt.Errorf("failed to update ArcaVolume: %w", MapKubernetesError(err, "ArcaVolume", info.VolumeID))
	}

	klog.Infof("Updated ArcaVolume %s", info.VolumeID)
	return nil
}

// GetVolume retrieves volume metadata
func (s *CRDStore) GetVolume(ctx context.Context, volumeID string) (*VolumeInfo, error) {
	ctx, cancel := context.WithTimeout(ctx, crudTimeout)
	defer cancel()
	if err := ctx.Err(); err != nil {
		return nil, err
	}

	av := &v1alpha1.ArcaVolume{}
	err := s.client.Get(ctx, client.ObjectKey{Name: volumeID}, av)
	if err != nil {
		// Map Kubernetes errors to typed store errors
		return nil, MapKubernetesError(err, "ArcaVolume", volumeID)
	}

	info := arcaVolumeToVolumeInfo(av)
	if err := validateVolumeInfo(info); err != nil {
		return nil, fmt.Errorf("invalid stored ArcaVolume %s: %w", volumeID, err)
	}
	return info, nil
}

// DeleteVolume removes volume metadata (idempotent)
func (s *CRDStore) DeleteVolume(ctx context.Context, volumeID string) error {
	ctx, cancel := context.WithTimeout(ctx, crudTimeout)
	defer cancel()
	if err := ctx.Err(); err != nil {
		return err
	}

	// Get the volume
	av := &v1alpha1.ArcaVolume{}
	err := s.client.Get(ctx, client.ObjectKey{Name: volumeID}, av)
	if err != nil {
		mapped := MapKubernetesError(err, "ArcaVolume", volumeID)
		// If not found, already deleted (idempotent)
		if IsNotFound(mapped) {
			klog.V(4).Infof("ArcaVolume %s already deleted", volumeID)
			return nil
		}
		// Other errors (e.g., unavailable) should be returned
		return fmt.Errorf("failed to get ArcaVolume for deletion: %w", mapped)
	}

	// Remove only this driver's finalizer (do not wipe other controllers' finalizers)
	if hasFinalizer(av.Finalizers, FinalizerArcaStorage) {
		av.Finalizers = removeFinalizer(av.Finalizers, FinalizerArcaStorage)
		if err := s.client.Update(ctx, av); err != nil {
			mapped := MapKubernetesError(err, "ArcaVolume", volumeID)
			if !IsNotFound(mapped) { // Ignore if already deleted
				klog.Warningf("Failed to remove finalizers from ArcaVolume %s: %v", volumeID, mapped)
			}
		}
	}

	// Delete the resource
	err = s.client.Delete(ctx, av)
	if err != nil {
		mapped := MapKubernetesError(err, "ArcaVolume", volumeID)
		// If not found, already deleted (idempotent)
		if IsNotFound(mapped) {
			klog.V(4).Infof("ArcaVolume %s already deleted during delete call", volumeID)
			return nil
		}
		// Other errors should be returned
		return fmt.Errorf("failed to delete ArcaVolume: %w", mapped)
	}

	klog.Infof("Deleted ArcaVolume %s", volumeID)
	return nil
}

// ListVolumes returns all volumes with optional pagination
func (s *CRDStore) ListVolumes(ctx context.Context, startingToken string, maxEntries int) ([]*VolumeInfo, string, error) {
	ctx, cancel := context.WithTimeout(ctx, listTimeout)
	defer cancel()
	if err := ctx.Err(); err != nil {
		return nil, "", err
	}

	avList := &v1alpha1.ArcaVolumeList{}
	listOpts := &client.ListOptions{
		Raw: &metav1.ListOptions{
			Continue: startingToken,
		},
	}
	if maxEntries > 0 {
		listOpts.Limit = int64(maxEntries)
	}

	if err := s.client.List(ctx, avList, listOpts); err != nil {
		return nil, "", fmt.Errorf("failed to list ArcaVolumes: %w", err)
	}

	result := make([]*VolumeInfo, 0, len(avList.Items))
	for i := range avList.Items {
		info := arcaVolumeToVolumeInfo(&avList.Items[i])
		if err := validateVolumeInfo(info); err != nil {
			return nil, "", fmt.Errorf("invalid stored ArcaVolume %s: %w", avList.Items[i].Name, err)
		}
		result = append(result, info)
	}

	// Return results in Kubernetes natural order to maintain pagination consistency
	// Sorting would invalidate the continue token since K8s paginates before our sort
	return result, avList.Continue, nil
}

// CreateSnapshot stores snapshot metadata as ArcaSnapshot CRD (idempotent)
func (s *CRDStore) CreateSnapshot(ctx context.Context, info *SnapshotInfo) error {
	ctx, cancel := context.WithTimeout(ctx, crudTimeout)
	defer cancel()
	if err := ctx.Err(); err != nil {
		return err
	}

	if err := validateSnapshotInfo(info); err != nil {
		snapshotID := "<nil>"
		if info != nil {
			snapshotID = info.SnapshotID
		}
		return fmt.Errorf("invalid ArcaSnapshot metadata for %s: %w", snapshotID, err)
	}

	as := snapshotInfoToArcaSnapshot(info)

	err := s.client.Create(ctx, as)
	if err != nil {
		// Map Kubernetes errors to typed store errors
		mapped := MapKubernetesError(err, "ArcaSnapshot", info.SnapshotID)

		// If already exists, this is idempotent - return the mapped error
		// so controller can check parameters
		if IsAlreadyExists(mapped) {
			return mapped
		}

		return fmt.Errorf("failed to create ArcaSnapshot: %w", mapped)
	}

	klog.Infof("Created ArcaSnapshot %s", info.SnapshotID)
	return nil
}

// UpdateSnapshotStatus updates the status subresource of a snapshot (uses /status endpoint)
func (s *CRDStore) UpdateSnapshotStatus(ctx context.Context, snapshotID string, readyToUse bool) error {
	ctx, cancel := context.WithTimeout(ctx, crudTimeout)
	defer cancel()
	if err := ctx.Err(); err != nil {
		return err
	}

	err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
		// Get the snapshot first to use the latest resourceVersion.
		as := &v1alpha1.ArcaSnapshot{}
		if err := s.client.Get(ctx, client.ObjectKey{Name: snapshotID}, as); err != nil {
			return err
		}

		// Update only the status subresource using Status() writer.
		as.Status.ReadyToUse = readyToUse
		return s.client.Status().Update(ctx, as)
	})
	if err != nil {
		return fmt.Errorf("failed to update snapshot status: %w", MapKubernetesError(err, "ArcaSnapshot", snapshotID))
	}

	klog.Infof("Updated ArcaSnapshot %s status: ReadyToUse=%v", snapshotID, readyToUse)
	return nil
}

// GetSnapshot retrieves snapshot metadata
func (s *CRDStore) GetSnapshot(ctx context.Context, snapshotID string) (*SnapshotInfo, error) {
	ctx, cancel := context.WithTimeout(ctx, crudTimeout)
	defer cancel()
	if err := ctx.Err(); err != nil {
		return nil, err
	}

	as := &v1alpha1.ArcaSnapshot{}
	err := s.client.Get(ctx, client.ObjectKey{Name: snapshotID}, as)
	if err != nil {
		// Map Kubernetes errors to typed store errors
		return nil, MapKubernetesError(err, "ArcaSnapshot", snapshotID)
	}

	info := arcaSnapshotToSnapshotInfo(as)
	if err := validateSnapshotInfo(info); err != nil {
		return nil, fmt.Errorf("invalid stored ArcaSnapshot %s: %w", snapshotID, err)
	}
	return info, nil
}

// DeleteSnapshot removes snapshot metadata (idempotent)
func (s *CRDStore) DeleteSnapshot(ctx context.Context, snapshotID string) error {
	ctx, cancel := context.WithTimeout(ctx, crudTimeout)
	defer cancel()
	if err := ctx.Err(); err != nil {
		return err
	}

	// Get the snapshot
	as := &v1alpha1.ArcaSnapshot{}
	err := s.client.Get(ctx, client.ObjectKey{Name: snapshotID}, as)
	if err != nil {
		mapped := MapKubernetesError(err, "ArcaSnapshot", snapshotID)
		// If not found, already deleted (idempotent)
		if IsNotFound(mapped) {
			klog.V(4).Infof("ArcaSnapshot %s already deleted", snapshotID)
			return nil
		}
		// Other errors (e.g., unavailable) should be returned
		return fmt.Errorf("failed to get ArcaSnapshot for deletion: %w", mapped)
	}

	// Remove only this driver's finalizer (do not wipe other controllers' finalizers)
	if hasFinalizer(as.Finalizers, FinalizerArcaStorage) {
		as.Finalizers = removeFinalizer(as.Finalizers, FinalizerArcaStorage)
		if err := s.client.Update(ctx, as); err != nil {
			mapped := MapKubernetesError(err, "ArcaSnapshot", snapshotID)
			if !IsNotFound(mapped) { // Ignore if already deleted
				klog.Warningf("Failed to remove finalizers from ArcaSnapshot %s: %v", snapshotID, mapped)
			}
		}
	}

	// Delete the resource
	err = s.client.Delete(ctx, as)
	if err != nil {
		mapped := MapKubernetesError(err, "ArcaSnapshot", snapshotID)
		// If not found, already deleted (idempotent)
		if IsNotFound(mapped) {
			klog.V(4).Infof("ArcaSnapshot %s already deleted during delete call", snapshotID)
			return nil
		}
		// Other errors should be returned
		return fmt.Errorf("failed to delete ArcaSnapshot: %w", mapped)
	}

	klog.Infof("Deleted ArcaSnapshot %s", snapshotID)
	return nil
}

// ListSnapshots returns all snapshots with optional filtering and pagination
func (s *CRDStore) ListSnapshots(ctx context.Context, sourceVolumeID, startingToken string, maxEntries int) ([]*SnapshotInfo, string, error) {
	ctx, cancel := context.WithTimeout(ctx, listTimeout)
	defer cancel()
	if err := ctx.Err(); err != nil {
		return nil, "", err
	}

	asList := &v1alpha1.ArcaSnapshotList{}
	listOpts := &client.ListOptions{
		Raw: &metav1.ListOptions{
			Continue: startingToken,
		},
	}
	if maxEntries > 0 {
		listOpts.Limit = int64(maxEntries)
	}

	// Add label selector if filtering by source volume
	if sourceVolumeID != "" {
		listOpts.LabelSelector, _ = metav1.LabelSelectorAsSelector(&metav1.LabelSelector{
			MatchLabels: map[string]string{
				"storage.arca.io/source-volume-id": sourceVolumeID,
			},
		})
	}

	if err := s.client.List(ctx, asList, listOpts); err != nil {
		return nil, "", fmt.Errorf("failed to list ArcaSnapshots: %w", err)
	}

	result := make([]*SnapshotInfo, 0, len(asList.Items))
	for i := range asList.Items {
		info := arcaSnapshotToSnapshotInfo(&asList.Items[i])
		if err := validateSnapshotInfo(info); err != nil {
			return nil, "", fmt.Errorf("invalid stored ArcaSnapshot %s: %w", asList.Items[i].Name, err)
		}
		result = append(result, info)
	}

	// Return results in Kubernetes natural order to maintain pagination consistency
	// Sorting would invalidate the continue token since K8s paginates before our sort
	return result, asList.Continue, nil
}
