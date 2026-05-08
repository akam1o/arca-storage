package driver

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/container-storage-interface/spec/lib/go/csi"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	mountutils "k8s.io/mount-utils"

	arcamount "github.com/akam1o/csi-arca-storage/pkg/mount"
)

type fakeMountSourceValidator struct {
	err error
}

func (v fakeMountSourceValidator) ValidateMountSource(targetPath, expectedSource string) error {
	return v.err
}

type mountSourceValidationCall struct {
	targetPath     string
	expectedSource string
}

type recordingMountSourceValidator struct {
	err   error
	errs  []error
	calls []mountSourceValidationCall
}

func (v *recordingMountSourceValidator) ValidateMountSource(targetPath, expectedSource string) error {
	v.calls = append(v.calls, mountSourceValidationCall{
		targetPath:     targetPath,
		expectedSource: expectedSource,
	})
	if len(v.errs) >= len(v.calls) {
		return v.errs[len(v.calls)-1]
	}
	return v.err
}

type fakeNodeMountManager struct {
	mu            sync.Mutex
	mountPath     string
	mountPathErr  error
	ensureStarted chan struct{}
	ensureRelease chan struct{}
	shouldUnmount bool
	ensureCalls   int
	getCalls      []string
	shouldCalls   []string
	unmountCalls  []string
}

func (m *fakeNodeMountManager) EnsureSVMMount(ctx context.Context, svmName, vip, exportRoot string, nfsMountOptions []string) (string, error) {
	m.mu.Lock()
	m.ensureCalls++
	ensureStarted := m.ensureStarted
	if ensureStarted != nil {
		m.ensureStarted = nil
	}
	ensureRelease := m.ensureRelease
	mountPath := m.mountPath
	m.mu.Unlock()

	if ensureStarted != nil {
		close(ensureStarted)
	}
	if ensureRelease != nil {
		<-ensureRelease
	}

	if mountPath == "" {
		return filepath.Join(os.TempDir(), svmName), nil
	}
	return mountPath, nil
}

func (m *fakeNodeMountManager) GetMountPath(svmName string) (string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	m.getCalls = append(m.getCalls, svmName)
	if m.mountPathErr != nil {
		return "", m.mountPathErr
	}
	if m.mountPath == "" {
		return filepath.Join(os.TempDir(), svmName), nil
	}
	return m.mountPath, nil
}

func (m *fakeNodeMountManager) ShouldUnmountSVM(ctx context.Context, svmName string) (bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	m.shouldCalls = append(m.shouldCalls, svmName)
	return m.shouldUnmount, nil
}

func (m *fakeNodeMountManager) UnmountSVM(ctx context.Context, svmName string) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	m.unmountCalls = append(m.unmountCalls, svmName)
	return nil
}

func testMountCapability() *csi.VolumeCapability {
	return &csi.VolumeCapability{
		AccessType: &csi.VolumeCapability_Mount{Mount: &csi.VolumeCapability_MountVolume{}},
		AccessMode: &csi.VolumeCapability_AccessMode{Mode: csi.VolumeCapability_AccessMode_SINGLE_NODE_WRITER},
	}
}

func TestNFSMountOptionsFromCapabilityUsesMountFlags(t *testing.T) {
	capability := &csi.VolumeCapability{
		AccessType: &csi.VolumeCapability_Mount{
			Mount: &csi.VolumeCapability_MountVolume{
				MountFlags: []string{"nfsvers=4.1", "rsize=262144", "ro", "bind", "remount", "rw", "soft", "resvport"},
			},
		},
	}

	got := nfsMountOptionsFromCapability(capability)
	want := []string{"vers=4.1", "rsize=262144", "wsize=1048576", "soft", "timeo=600", "retrans=2", "resvport"}

	if !reflect.DeepEqual(got, want) {
		t.Fatalf("unexpected NFS mount options: got %v want %v", got, want)
	}
}

func TestNFSMountOptionsFromCapabilityMergesCustomFlagsWithDefaults(t *testing.T) {
	capability := &csi.VolumeCapability{
		AccessType: &csi.VolumeCapability_Mount{
			Mount: &csi.VolumeCapability_MountVolume{
				MountFlags: []string{"nconnect=8"},
			},
		},
	}

	got := nfsMountOptionsFromCapability(capability)
	want := []string{"vers=4.2", "rsize=1048576", "wsize=1048576", "hard", "timeo=600", "retrans=2", "noresvport", "nconnect=8"}

	if !reflect.DeepEqual(got, want) {
		t.Fatalf("unexpected NFS mount options: got %v want %v", got, want)
	}
}

func TestNFSMountOptionsFromCapabilityDefaultsWhenNoFlags(t *testing.T) {
	capability := &csi.VolumeCapability{
		AccessType: &csi.VolumeCapability_Mount{
			Mount: &csi.VolumeCapability_MountVolume{},
		},
	}

	got := nfsMountOptionsFromCapability(capability)
	if len(got) == 0 {
		t.Fatal("expected default NFS mount options")
	}
}

func TestBindMountOptionsStayLocalOnly(t *testing.T) {
	if got, want := bindMountOptions(), []string{"bind"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("unexpected bind mount options: got %v want %v", got, want)
	}

	if got, want := readonlyBindRemountOptions(), []string{"bind", "remount", "ro"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("unexpected read-only remount options: got %v want %v", got, want)
	}
}

func TestValidateSVMNameRejectsPathTraversal(t *testing.T) {
	for _, name := range []string{"../tenant", "tenant/a", "tenant a", "-tenant"} {
		if err := validateSVMName(name); err == nil {
			t.Fatalf("expected invalid SVM name %q to be rejected", name)
		}
	}

	if err := validateSVMName("tenant-a_1.example"); err != nil {
		t.Fatalf("expected valid SVM name to be accepted: %v", err)
	}
}

func TestValidateVolumePathRejectsRootAliasesAndTraversal(t *testing.T) {
	for _, path := range []string{"", ".", "./", "dir/..", "dir/../vol", "dir/.", "dir//vol", "/vol"} {
		if err := validateVolumePath(path); err == nil {
			t.Fatalf("expected invalid volume path %q to be rejected", path)
		}
	}

	for _, path := range []string{"pvc-1234", "volumes/pvc-1234"} {
		if err := validateVolumePath(path); err != nil {
			t.Fatalf("expected valid volume path %q to be accepted: %v", path, err)
		}
	}
}

func TestNodeGetVolumeStatsReturnsFilesystemUsage(t *testing.T) {
	tmp := t.TempDir()
	volumePath := filepath.Join(tmp, "volume")
	if err := os.MkdirAll(volumePath, 0750); err != nil {
		t.Fatalf("failed to create volume path: %v", err)
	}
	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	driver := &Driver{
		mode:         "node",
		nodeID:       "node-a",
		nodeState:    nodeState,
		mountManager: new(arcamount.MountManager),
	}

	resp, err := driver.NodeGetVolumeStats(context.Background(), &csi.NodeGetVolumeStatsRequest{
		VolumeId:   "vol-a",
		VolumePath: volumePath,
	})
	if err != nil {
		t.Fatalf("NodeGetVolumeStats() error = %v", err)
	}

	var bytesUsage, inodesUsage *csi.VolumeUsage
	for _, usage := range resp.GetUsage() {
		switch usage.GetUnit() {
		case csi.VolumeUsage_BYTES:
			bytesUsage = usage
		case csi.VolumeUsage_INODES:
			inodesUsage = usage
		}
	}
	if bytesUsage == nil {
		t.Fatal("bytes usage entry not returned")
	}
	if bytesUsage.GetTotal() <= 0 {
		t.Fatalf("bytes total = %d, want > 0", bytesUsage.GetTotal())
	}
	if bytesUsage.GetAvailable() < 0 || bytesUsage.GetAvailable() > bytesUsage.GetTotal() {
		t.Fatalf("bytes available = %d, total = %d", bytesUsage.GetAvailable(), bytesUsage.GetTotal())
	}
	if inodesUsage == nil {
		t.Fatal("inodes usage entry not returned")
	}
	if inodesUsage.GetTotal() <= 0 {
		t.Fatalf("inodes total = %d, want > 0", inodesUsage.GetTotal())
	}
	if inodesUsage.GetAvailable() < 0 || inodesUsage.GetAvailable() > inodesUsage.GetTotal() {
		t.Fatalf("inodes available = %d, total = %d", inodesUsage.GetAvailable(), inodesUsage.GetTotal())
	}
}

func TestNodeStageSerializesSVMMountLifecycle(t *testing.T) {
	tmp := t.TempDir()
	ensureStarted := make(chan struct{})
	ensureRelease := make(chan struct{})
	released := false
	defer func() {
		if !released {
			close(ensureRelease)
		}
	}()

	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	mountManager := &fakeNodeMountManager{
		mountPath:     filepath.Join(tmp, "svm-mount"),
		ensureStarted: ensureStarted,
		ensureRelease: ensureRelease,
	}
	driver := &Driver{
		mode:         "node",
		nodeID:       "node-a",
		nodeState:    nodeState,
		mountManager: mountManager,
		nodeMounter:  mountutils.NewFakeMounter(nil),
	}

	stage := func(volumeID, stagingTargetPath string) error {
		_, err := driver.NodeStageVolume(context.Background(), &csi.NodeStageVolumeRequest{
			VolumeId:          volumeID,
			StagingTargetPath: stagingTargetPath,
			VolumeCapability:  testMountCapability(),
			VolumeContext: map[string]string{
				volumeContextSVM:        "svm-a",
				volumeContextVIP:        "10.0.0.1",
				volumeContextVolumePath: filepath.Join("volumes", volumeID),
			},
		})
		return err
	}

	errCh := make(chan error, 2)
	go func() {
		errCh <- stage("vol-a", filepath.Join(tmp, "stage-a"))
	}()

	select {
	case <-ensureStarted:
	case <-time.After(2 * time.Second):
		t.Fatal("first stage did not reach EnsureSVMMount")
	}

	secondStarted := make(chan struct{})
	go func() {
		close(secondStarted)
		errCh <- stage("vol-b", filepath.Join(tmp, "stage-b"))
	}()
	<-secondStarted

	time.Sleep(50 * time.Millisecond)
	mountManager.mu.Lock()
	ensureCalls := mountManager.ensureCalls
	mountManager.mu.Unlock()
	if ensureCalls != 1 {
		t.Fatalf("second stage reached EnsureSVMMount before first stage finished: calls=%d", ensureCalls)
	}

	close(ensureRelease)
	released = true

	for i := 0; i < 2; i++ {
		select {
		case err := <-errCh:
			if err != nil {
				t.Fatalf("NodeStageVolume failed: %v", err)
			}
		case <-time.After(2 * time.Second):
			t.Fatal("NodeStageVolume did not finish")
		}
	}
	mountManager.mu.Lock()
	ensureCalls = mountManager.ensureCalls
	mountManager.mu.Unlock()
	if ensureCalls != 2 {
		t.Fatalf("EnsureSVMMount calls = %d, want 2", ensureCalls)
	}
	if got := nodeState.CountStagedVolumesForSVM("svm-a"); got != 2 {
		t.Fatalf("staged volumes for svm-a = %d, want 2", got)
	}
}

func TestNodeStageCleansUpSVMMountWhenStagingDirectoryCreationFails(t *testing.T) {
	tmp := t.TempDir()
	blockingFile := filepath.Join(tmp, "not-a-directory")
	if err := os.WriteFile(blockingFile, []byte("blocked"), 0600); err != nil {
		t.Fatalf("failed to create blocking file: %v", err)
	}

	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	mountManager := &fakeNodeMountManager{
		mountPath:     filepath.Join(tmp, "svm-mount"),
		shouldUnmount: true,
	}
	driver := &Driver{
		mode:         "node",
		nodeID:       "node-a",
		nodeState:    nodeState,
		mountManager: mountManager,
		nodeMounter:  mountutils.NewFakeMounter(nil),
	}

	_, err = driver.NodeStageVolume(context.Background(), &csi.NodeStageVolumeRequest{
		VolumeId:          "vol-a",
		StagingTargetPath: filepath.Join(blockingFile, "stage"),
		VolumeCapability:  testMountCapability(),
		VolumeContext: map[string]string{
			volumeContextSVM:        "svm-a",
			volumeContextVIP:        "10.0.0.1",
			volumeContextVolumePath: "volumes/vol-a",
		},
	})
	if status.Code(err) != codes.Internal {
		t.Fatalf("expected Internal, got %v", err)
	}
	if mountManager.ensureCalls != 1 {
		t.Fatalf("EnsureSVMMount calls = %d, want 1", mountManager.ensureCalls)
	}
	if !reflect.DeepEqual(mountManager.shouldCalls, []string{"svm-a"}) {
		t.Fatalf("ShouldUnmountSVM calls = %#v", mountManager.shouldCalls)
	}
	if !reflect.DeepEqual(mountManager.unmountCalls, []string{"svm-a"}) {
		t.Fatalf("UnmountSVM calls = %#v", mountManager.unmountCalls)
	}
}

func TestNodeUnstageUsesFreshNodeStateForSVMCleanup(t *testing.T) {
	tmp := t.TempDir()
	stateFile := filepath.Join(tmp, "state.json")
	writer, err := arcamount.NewNodeState(stateFile)
	if err != nil {
		t.Fatalf("failed to create writer node state: %v", err)
	}
	reader, err := arcamount.NewNodeState(stateFile)
	if err != nil {
		t.Fatalf("failed to create reader node state: %v", err)
	}

	stagingPath := filepath.Join(tmp, "stage")
	if err := os.MkdirAll(stagingPath, 0750); err != nil {
		t.Fatalf("failed to create staging path: %v", err)
	}
	mountedStagingPath, err := filepath.EvalSymlinks(stagingPath)
	if err != nil {
		t.Fatalf("failed to resolve staging path: %v", err)
	}
	if err := writer.RecordVolumeStaging(
		"vol-a",
		"svm-a",
		"10.0.0.1",
		"",
		"volumes/vol-a",
		stagingPath,
		nil,
	); err != nil {
		t.Fatalf("failed to record staging: %v", err)
	}

	mountManager := &fakeNodeMountManager{shouldUnmount: true}
	driver := &Driver{
		mode:         "node",
		nodeID:       "node-a",
		nodeState:    reader,
		mountManager: mountManager,
		nodeMounter: mountutils.NewFakeMounter([]mountutils.MountPoint{
			{Device: "/svm/volumes/vol-a", Path: mountedStagingPath, Type: "", Opts: []string{"bind"}},
		}),
	}

	_, err = driver.NodeUnstageVolume(context.Background(), &csi.NodeUnstageVolumeRequest{
		VolumeId:          "vol-a",
		StagingTargetPath: stagingPath,
	})
	if err != nil {
		t.Fatalf("NodeUnstageVolume failed: %v", err)
	}
	if !reflect.DeepEqual(mountManager.shouldCalls, []string{"svm-a"}) {
		t.Fatalf("ShouldUnmountSVM calls = %#v", mountManager.shouldCalls)
	}
	if !reflect.DeepEqual(mountManager.unmountCalls, []string{"svm-a"}) {
		t.Fatalf("UnmountSVM calls = %#v", mountManager.unmountCalls)
	}

	reloaded, err := arcamount.NewNodeState(stateFile)
	if err != nil {
		t.Fatalf("failed to reload node state: %v", err)
	}
	if got := reloaded.CountStagedVolumesForSVM("svm-a"); got != 0 {
		t.Fatalf("staged volumes for svm-a = %d, want 0", got)
	}
}

func TestNodePublishRejectsUnrecordedExistingMount(t *testing.T) {
	tmp := t.TempDir()
	targetPath := filepath.Join(tmp, "target")
	stagingPath := filepath.Join(tmp, "staging")
	if err := os.MkdirAll(targetPath, 0750); err != nil {
		t.Fatalf("failed to create target path: %v", err)
	}
	if err := os.MkdirAll(stagingPath, 0750); err != nil {
		t.Fatalf("failed to create staging path: %v", err)
	}
	mountedTargetPath, err := filepath.EvalSymlinks(targetPath)
	if err != nil {
		t.Fatalf("failed to resolve target path: %v", err)
	}
	mountedStagingPath, err := filepath.EvalSymlinks(stagingPath)
	if err != nil {
		t.Fatalf("failed to resolve staging path: %v", err)
	}

	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	fakeMounter := mountutils.NewFakeMounter([]mountutils.MountPoint{
		{Device: mountedStagingPath, Path: mountedTargetPath, Type: "", Opts: []string{"bind"}},
	})
	driver := &Driver{
		mode:                 "node",
		nodeID:               "node-a",
		nodeState:            nodeState,
		mountManager:         new(arcamount.MountManager),
		nodeMounter:          fakeMounter,
		mountSourceValidator: fakeMountSourceValidator{},
	}

	_, err = driver.NodePublishVolume(context.Background(), &csi.NodePublishVolumeRequest{
		VolumeId:          "vol-a",
		StagingTargetPath: stagingPath,
		TargetPath:        targetPath,
		VolumeCapability: &csi.VolumeCapability{
			AccessType: &csi.VolumeCapability_Mount{Mount: &csi.VolumeCapability_MountVolume{}},
			AccessMode: &csi.VolumeCapability_AccessMode{Mode: csi.VolumeCapability_AccessMode_SINGLE_NODE_WRITER},
		},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("expected FailedPrecondition, got %v", err)
	}
	if !strings.Contains(err.Error(), "not recorded for volume") {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestNodePublishRejectsFirstPublishWithMismatchedStagingPath(t *testing.T) {
	tmp := t.TempDir()
	targetPath := filepath.Join(tmp, "target")
	recordedStagingPath := filepath.Join(tmp, "recorded-staging")
	requestedStagingPath := filepath.Join(tmp, "requested-staging")
	if err := os.MkdirAll(recordedStagingPath, 0750); err != nil {
		t.Fatalf("failed to create recorded staging path: %v", err)
	}
	if err := os.MkdirAll(requestedStagingPath, 0750); err != nil {
		t.Fatalf("failed to create requested staging path: %v", err)
	}
	mountedRequestedStagingPath, err := filepath.EvalSymlinks(requestedStagingPath)
	if err != nil {
		t.Fatalf("failed to resolve requested staging path: %v", err)
	}

	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	if err := nodeState.RecordVolumeStaging("vol-a", "svm-a", "10.0.0.1", "", "volumes/vol-a", recordedStagingPath, nil); err != nil {
		t.Fatalf("failed to record staging: %v", err)
	}

	fakeMounter := mountutils.NewFakeMounter([]mountutils.MountPoint{
		{Device: "/svm/volumes/vol-a", Path: mountedRequestedStagingPath, Type: "", Opts: []string{"bind"}},
	})
	driver := &Driver{
		mode:                 "node",
		nodeID:               "node-a",
		nodeState:            nodeState,
		mountManager:         new(arcamount.MountManager),
		nodeMounter:          fakeMounter,
		mountSourceValidator: fakeMountSourceValidator{},
	}

	_, err = driver.NodePublishVolume(context.Background(), &csi.NodePublishVolumeRequest{
		VolumeId:          "vol-a",
		StagingTargetPath: requestedStagingPath,
		TargetPath:        targetPath,
		VolumeCapability:  testMountCapability(),
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("expected FailedPrecondition, got %v", err)
	}
	if !strings.Contains(err.Error(), "staging path mismatch") {
		t.Fatalf("unexpected error: %v", err)
	}
	if nodeState.HasVolumePublish("vol-a", targetPath) {
		t.Fatal("target should not be recorded after rejected publish")
	}
}

func TestNodePublishRejectsFirstPublishWhenStagingPathIsNotMounted(t *testing.T) {
	tmp := t.TempDir()
	targetPath := filepath.Join(tmp, "target")
	stagingPath := filepath.Join(tmp, "staging")
	if err := os.MkdirAll(stagingPath, 0750); err != nil {
		t.Fatalf("failed to create staging path: %v", err)
	}

	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	if err := nodeState.RecordVolumeStaging("vol-a", "svm-a", "10.0.0.1", "", "volumes/vol-a", stagingPath, nil); err != nil {
		t.Fatalf("failed to record staging: %v", err)
	}

	driver := &Driver{
		mode:                 "node",
		nodeID:               "node-a",
		nodeState:            nodeState,
		mountManager:         new(arcamount.MountManager),
		nodeMounter:          mountutils.NewFakeMounter(nil),
		mountSourceValidator: fakeMountSourceValidator{},
	}

	_, err = driver.NodePublishVolume(context.Background(), &csi.NodePublishVolumeRequest{
		VolumeId:          "vol-a",
		StagingTargetPath: stagingPath,
		TargetPath:        targetPath,
		VolumeCapability:  testMountCapability(),
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("expected FailedPrecondition, got %v", err)
	}
	if !strings.Contains(err.Error(), "is not mounted") {
		t.Fatalf("unexpected error: %v", err)
	}
	if nodeState.HasVolumePublish("vol-a", targetPath) {
		t.Fatal("target should not be recorded after rejected publish")
	}
}

func TestNodePublishRejectsFirstPublishWithWrongStagingSource(t *testing.T) {
	tmp := t.TempDir()
	targetPath := filepath.Join(tmp, "target")
	stagingPath := filepath.Join(tmp, "staging")
	svmMountPath := filepath.Join(tmp, "svm")
	volumePath := "volumes/vol-a"
	if err := os.MkdirAll(stagingPath, 0750); err != nil {
		t.Fatalf("failed to create staging path: %v", err)
	}
	mountedStagingPath, err := filepath.EvalSymlinks(stagingPath)
	if err != nil {
		t.Fatalf("failed to resolve staging path: %v", err)
	}

	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	if err := nodeState.RecordVolumeStaging("vol-a", "svm-a", "10.0.0.1", "", volumePath, stagingPath, nil); err != nil {
		t.Fatalf("failed to record staging: %v", err)
	}

	expectedSource := filepath.Join(svmMountPath, volumePath)
	validator := &recordingMountSourceValidator{
		err: fmt.Errorf("mount source mismatch: active=%s requested=%s", filepath.Join(tmp, "stale-volume"), expectedSource),
	}
	driver := &Driver{
		mode:         "node",
		nodeID:       "node-a",
		nodeState:    nodeState,
		mountManager: &fakeNodeMountManager{mountPath: svmMountPath},
		nodeMounter: mountutils.NewFakeMounter([]mountutils.MountPoint{
			{Device: filepath.Join(tmp, "stale-volume"), Path: mountedStagingPath, Type: "", Opts: []string{"bind"}},
		}),
		mountSourceValidator: validator,
	}

	_, err = driver.NodePublishVolume(context.Background(), &csi.NodePublishVolumeRequest{
		VolumeId:          "vol-a",
		StagingTargetPath: stagingPath,
		TargetPath:        targetPath,
		VolumeCapability:  testMountCapability(),
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("expected FailedPrecondition, got %v", err)
	}
	if !strings.Contains(err.Error(), "does not match recorded source") {
		t.Fatalf("unexpected error: %v", err)
	}
	if !reflect.DeepEqual(validator.calls, []mountSourceValidationCall{{targetPath: stagingPath, expectedSource: expectedSource}}) {
		t.Fatalf("unexpected source validation calls: %#v", validator.calls)
	}
	if nodeState.HasVolumePublish("vol-a", targetPath) {
		t.Fatal("target should not be recorded after rejected publish")
	}
}

func TestNodePublishRejectsExistingMountWithDifferentSource(t *testing.T) {
	tmp := t.TempDir()
	targetPath := filepath.Join(tmp, "target")
	stagingPath := filepath.Join(tmp, "staging")
	if err := os.MkdirAll(targetPath, 0750); err != nil {
		t.Fatalf("failed to create target path: %v", err)
	}
	if err := os.MkdirAll(stagingPath, 0750); err != nil {
		t.Fatalf("failed to create staging path: %v", err)
	}
	mountedTargetPath, err := filepath.EvalSymlinks(targetPath)
	if err != nil {
		t.Fatalf("failed to resolve target path: %v", err)
	}

	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	if err := nodeState.RecordVolumeStaging("vol-a", "svm-a", "10.0.0.1", "", "volumes/vol-a", stagingPath, nil); err != nil {
		t.Fatalf("failed to record staging: %v", err)
	}
	if err := nodeState.RecordVolumePublish("vol-a", targetPath, false); err != nil {
		t.Fatalf("failed to record publish: %v", err)
	}

	fakeMounter := mountutils.NewFakeMounter([]mountutils.MountPoint{
		{Device: "/var/lib/kubelet/plugins/stale-volume", Path: mountedTargetPath, Type: "", Opts: []string{"bind"}},
	})
	driver := &Driver{
		mode:                 "node",
		nodeID:               "node-a",
		nodeState:            nodeState,
		mountManager:         new(arcamount.MountManager),
		nodeMounter:          fakeMounter,
		mountSourceValidator: fakeMountSourceValidator{err: fmt.Errorf("mount source mismatch: active=stale requested=%s", stagingPath)},
	}

	_, err = driver.NodePublishVolume(context.Background(), &csi.NodePublishVolumeRequest{
		VolumeId:          "vol-a",
		StagingTargetPath: stagingPath,
		TargetPath:        targetPath,
		VolumeCapability: &csi.VolumeCapability{
			AccessType: &csi.VolumeCapability_Mount{Mount: &csi.VolumeCapability_MountVolume{}},
			AccessMode: &csi.VolumeCapability_AccessMode{Mode: csi.VolumeCapability_AccessMode_SINGLE_NODE_WRITER},
		},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("expected FailedPrecondition, got %v", err)
	}
	if !strings.Contains(err.Error(), "mount source mismatch") {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestNodePublishRejectsExistingMountWithWrongStagingSource(t *testing.T) {
	tmp := t.TempDir()
	targetPath := filepath.Join(tmp, "target")
	stagingPath := filepath.Join(tmp, "staging")
	svmMountPath := filepath.Join(tmp, "svm")
	volumePath := "volumes/vol-a"
	staleSource := filepath.Join(tmp, "stale-volume")
	if err := os.MkdirAll(targetPath, 0750); err != nil {
		t.Fatalf("failed to create target path: %v", err)
	}
	if err := os.MkdirAll(stagingPath, 0750); err != nil {
		t.Fatalf("failed to create staging path: %v", err)
	}
	mountedTargetPath, err := filepath.EvalSymlinks(targetPath)
	if err != nil {
		t.Fatalf("failed to resolve target path: %v", err)
	}
	mountedStagingPath, err := filepath.EvalSymlinks(stagingPath)
	if err != nil {
		t.Fatalf("failed to resolve staging path: %v", err)
	}

	nodeState, err := arcamount.NewNodeState(filepath.Join(tmp, "state.json"))
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	if err := nodeState.RecordVolumeStaging("vol-a", "svm-a", "10.0.0.1", "", volumePath, stagingPath, nil); err != nil {
		t.Fatalf("failed to record staging: %v", err)
	}
	if err := nodeState.RecordVolumePublish("vol-a", targetPath, false); err != nil {
		t.Fatalf("failed to record publish: %v", err)
	}

	expectedSource := filepath.Join(svmMountPath, volumePath)
	validator := &recordingMountSourceValidator{
		errs: []error{
			nil,
			fmt.Errorf("mount source mismatch: active=%s requested=%s", staleSource, expectedSource),
		},
	}
	driver := &Driver{
		mode:         "node",
		nodeID:       "node-a",
		nodeState:    nodeState,
		mountManager: &fakeNodeMountManager{mountPath: svmMountPath},
		nodeMounter: mountutils.NewFakeMounter([]mountutils.MountPoint{
			{Device: mountedStagingPath, Path: mountedTargetPath, Type: "", Opts: []string{"bind"}},
			{Device: staleSource, Path: mountedStagingPath, Type: "", Opts: []string{"bind"}},
		}),
		mountSourceValidator: validator,
	}

	_, err = driver.NodePublishVolume(context.Background(), &csi.NodePublishVolumeRequest{
		VolumeId:          "vol-a",
		StagingTargetPath: stagingPath,
		TargetPath:        targetPath,
		VolumeCapability:  testMountCapability(),
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("expected FailedPrecondition, got %v", err)
	}
	if !strings.Contains(err.Error(), "does not match recorded source") {
		t.Fatalf("unexpected error: %v", err)
	}

	wantCalls := []mountSourceValidationCall{
		{targetPath: targetPath, expectedSource: stagingPath},
		{targetPath: stagingPath, expectedSource: expectedSource},
	}
	if !reflect.DeepEqual(validator.calls, wantCalls) {
		t.Fatalf("unexpected source validation calls: got %#v want %#v", validator.calls, wantCalls)
	}
}

func TestNodePublishRejectsExistingMountWithReadonlyMismatch(t *testing.T) {
	tmp := t.TempDir()
	targetPath := filepath.Join(tmp, "target")
	stagingPath := filepath.Join(tmp, "staging")
	svmMountPath := filepath.Join(tmp, "svm")
	sourcePath := filepath.Join(svmMountPath, "volumes/vol-a")
	if err := os.MkdirAll(targetPath, 0750); err != nil {
		t.Fatalf("failed to create target path: %v", err)
	}
	if err := os.MkdirAll(stagingPath, 0750); err != nil {
		t.Fatalf("failed to create staging path: %v", err)
	}
	mountedTargetPath, err := filepath.EvalSymlinks(targetPath)
	if err != nil {
		t.Fatalf("failed to resolve target path: %v", err)
	}
	mountedStagingPath, err := filepath.EvalSymlinks(stagingPath)
	if err != nil {
		t.Fatalf("failed to resolve staging path: %v", err)
	}

	stateFile := filepath.Join(tmp, "state.json")
	stateData := map[string]any{
		"volumes": map[string]any{
			"vol-a": map[string]any{
				"volume_id":       "vol-a",
				"svm_name":        "svm-a",
				"vip":             "10.0.0.1",
				"volume_path":     "volumes/vol-a",
				"staging_path":    stagingPath,
				"published_paths": []string{targetPath},
			},
		},
	}
	rawState, err := json.Marshal(stateData)
	if err != nil {
		t.Fatalf("failed to marshal legacy node state: %v", err)
	}
	if err := os.WriteFile(stateFile, rawState, 0600); err != nil {
		t.Fatalf("failed to write legacy node state: %v", err)
	}

	nodeState, err := arcamount.NewNodeState(stateFile)
	if err != nil {
		t.Fatalf("failed to create node state: %v", err)
	}
	fakeMounter := mountutils.NewFakeMounter([]mountutils.MountPoint{
		{Device: mountedStagingPath, Path: mountedTargetPath, Type: "", Opts: []string{"bind"}},
		{Device: sourcePath, Path: mountedStagingPath, Type: "", Opts: []string{"bind"}},
	})
	driver := &Driver{
		mode:                 "node",
		nodeID:               "node-a",
		nodeState:            nodeState,
		mountManager:         &fakeNodeMountManager{mountPath: svmMountPath},
		nodeMounter:          fakeMounter,
		mountSourceValidator: fakeMountSourceValidator{},
	}

	_, err = driver.NodePublishVolume(context.Background(), &csi.NodePublishVolumeRequest{
		VolumeId:          "vol-a",
		StagingTargetPath: stagingPath,
		TargetPath:        targetPath,
		Readonly:          true,
		VolumeCapability: &csi.VolumeCapability{
			AccessType: &csi.VolumeCapability_Mount{Mount: &csi.VolumeCapability_MountVolume{}},
			AccessMode: &csi.VolumeCapability_AccessMode{Mode: csi.VolumeCapability_AccessMode_SINGLE_NODE_WRITER},
		},
	})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("expected FailedPrecondition, got %v", err)
	}
	if !strings.Contains(err.Error(), "readonly mismatch") {
		t.Fatalf("unexpected error: %v", err)
	}
}
