package mount

import "strings"

func MergeNFSMountOptions(overrides []string) []string {
	result := GetDefaultNFSOptions()
	positions := make(map[string]int, len(result)+len(overrides))
	for i, opt := range result {
		positions[nfsMountOptionKey(opt)] = i
	}

	for _, raw := range overrides {
		opt := canonicalNFSMountOption(strings.TrimSpace(raw))
		if isLocalMountOption(opt) {
			continue
		}

		key := nfsMountOptionKey(opt)
		if idx, exists := positions[key]; exists {
			result[idx] = opt
			continue
		}
		positions[key] = len(result)
		result = append(result, opt)
	}

	return result
}

func normalizeNFSMountOptions(options []string) []string {
	return MergeNFSMountOptions(options)
}

func cloneMountOptions(options []string) []string {
	return append([]string(nil), options...)
}

func sameMountOptions(a, b []string) bool {
	left := mountOptionMap(a)
	right := mountOptionMap(b)
	if len(left) != len(right) {
		return false
	}
	for key, leftValue := range left {
		if right[key] != leftValue {
			return false
		}
	}
	return true
}

func mountOptionMap(options []string) map[string]string {
	normalized := normalizeNFSMountOptions(options)
	result := make(map[string]string, len(normalized))
	for _, opt := range normalized {
		result[nfsMountOptionKey(opt)] = opt
	}
	return result
}

func canonicalNFSMountOption(opt string) string {
	key, value, hasValue := strings.Cut(opt, "=")
	if hasValue && key == "nfsvers" {
		return "vers=" + value
	}
	return opt
}

func nfsMountOptionKey(opt string) string {
	key, _, _ := strings.Cut(opt, "=")
	switch key {
	case "nfsvers":
		return "vers"
	case "hard", "soft":
		return "retry-mode"
	case "resvport", "noresvport":
		return "reserved-port"
	default:
		return key
	}
}

func isLocalMountOption(opt string) bool {
	switch opt {
	case "", "bind", "remount", "ro", "rw":
		return true
	default:
		return false
	}
}
