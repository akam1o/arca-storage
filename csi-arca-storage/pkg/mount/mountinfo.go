package mount

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

const procSelfMountInfoPath = "/proc/self/mountinfo"

// MountSourceValidator verifies that an existing mount point backs the expected source.
type MountSourceValidator interface {
	ValidateMountSource(targetPath, expectedSource string) error
}

// ProcMountInfoSourceValidator validates mounts using /proc/self/mountinfo.
type ProcMountInfoSourceValidator struct {
	MountInfoPath string
}

type mountInfoEntry struct {
	Root       string
	MountPoint string
	FSType     string
	Source     string
}

func (v ProcMountInfoSourceValidator) ValidateMountSource(targetPath, expectedSource string) error {
	path := v.MountInfoPath
	if path == "" {
		path = procSelfMountInfoPath
	}

	entries, err := readMountInfoEntries(path)
	if err != nil {
		return err
	}
	return validateMountSourceFromEntries(entries, targetPath, expectedSource)
}

func readMountInfoEntries(path string) ([]mountInfoEntry, error) {
	data, err := os.ReadFile(path) // #nosec G304 -- production reads /proc/self/mountinfo; tests inject temp files.
	if err != nil {
		return nil, fmt.Errorf("failed to read mountinfo: %w", err)
	}
	return parseMountInfo(string(data))
}

func parseMountInfo(content string) ([]mountInfoEntry, error) {
	var entries []mountInfoEntry
	for lineNumber, line := range strings.Split(content, "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}

		fields := strings.Fields(line)
		separator := -1
		for i, field := range fields {
			if field == "-" {
				separator = i
				break
			}
		}
		if separator < 6 || separator+3 >= len(fields) {
			return nil, fmt.Errorf("invalid mountinfo line %d", lineNumber+1)
		}

		entries = append(entries, mountInfoEntry{
			Root:       cleanMountPath(unescapeMountInfo(fields[3])),
			MountPoint: cleanMountPath(unescapeMountInfo(fields[4])),
			FSType:     fields[separator+1],
			Source:     unescapeMountInfo(fields[separator+2]),
		})
	}
	return entries, nil
}

func validateMountSourceFromEntries(entries []mountInfoEntry, targetPath, expectedSource string) error {
	targetEntry, ok := findMountInfoEntry(entries, targetPath)
	if !ok {
		return fmt.Errorf("mount point %s is mounted but no mountinfo entry was found", targetPath)
	}

	if !filepath.IsAbs(expectedSource) {
		if targetEntry.Source != expectedSource {
			return fmt.Errorf("mount source mismatch: active=%s requested=%s", targetEntry.Source, expectedSource)
		}
		return nil
	}

	expectedRef, err := resolveMountSourceReference(entries, expectedSource)
	if err != nil {
		return err
	}

	if targetEntry.Source != expectedRef.Source || targetEntry.Root != expectedRef.Root {
		return fmt.Errorf(
			"mount source mismatch: active=%s root=%s requested=%s root=%s",
			targetEntry.Source,
			targetEntry.Root,
			expectedRef.Source,
			expectedRef.Root,
		)
	}
	return nil
}

type mountSourceReference struct {
	Source string
	Root   string
}

func resolveMountSourceReference(entries []mountInfoEntry, sourcePath string) (mountSourceReference, error) {
	normalizedSource := cleanMountPath(sourcePath)
	var best *mountInfoEntry
	for i := range entries {
		entry := &entries[i]
		if !pathWithin(normalizedSource, entry.MountPoint) {
			continue
		}
		if best == nil || len(entry.MountPoint) > len(best.MountPoint) {
			best = entry
		}
	}
	if best == nil {
		return mountSourceReference{}, fmt.Errorf("source path %s is not under any mounted filesystem", sourcePath)
	}

	rel, err := filepath.Rel(best.MountPoint, normalizedSource)
	if err != nil {
		return mountSourceReference{}, fmt.Errorf("failed to resolve source path %s: %w", sourcePath, err)
	}

	return mountSourceReference{
		Source: best.Source,
		Root:   joinMountRoot(best.Root, rel),
	}, nil
}

func findMountInfoEntry(entries []mountInfoEntry, targetPath string) (mountInfoEntry, bool) {
	normalizedTarget := cleanMountPath(targetPath)
	var found mountInfoEntry
	ok := false
	for _, entry := range entries {
		if entry.MountPoint != normalizedTarget {
			continue
		}
		found = entry
		ok = true
	}
	return found, ok
}

func pathWithin(path, base string) bool {
	rel, err := filepath.Rel(base, path)
	if err != nil {
		return false
	}
	return rel == "." || (rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)))
}

func joinMountRoot(root, rel string) string {
	cleanRoot := cleanMountPath(root)
	if rel == "." || rel == "" {
		return cleanRoot
	}
	rel = filepath.ToSlash(filepath.Clean(rel))
	if cleanRoot == "/" {
		return cleanMountPath("/" + rel)
	}
	return cleanMountPath(cleanRoot + "/" + rel)
}

func cleanMountPath(path string) string {
	if path == "" {
		return "/"
	}
	return filepath.Clean(path)
}

func unescapeMountInfo(value string) string {
	replacer := strings.NewReplacer(
		`\\`, `\`,
		`\040`, " ",
		`\011`, "\t",
		`\012`, "\n",
		`\134`, `\`,
	)
	return replacer.Replace(value)
}
