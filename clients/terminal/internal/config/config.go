package config

import (
	"sort"
	"strconv"
	"strings"
)

type Config struct{ BootstrapFD int }

func ParseBootstrapFD(value string, fallback int) int {
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed < 0 {
		return fallback
	}
	return parsed
}

// SanitizedEnvironment keeps only renderer/locale hints. Credentials and
// application configuration are delivered through the one-shot bootstrap FD.
func SanitizedEnvironment(source []string, bootstrapFD int) []string {
	allowed := map[string]bool{
		"TERM": true, "COLORTERM": true, "LANG": true, "NO_COLOR": true,
		"TMUX": true, "SSH_TTY": true,
	}
	result := make([]string, 0, 8)
	for _, entry := range source {
		key, _, found := strings.Cut(entry, "=")
		if !found || (!allowed[key] && !strings.HasPrefix(key, "LC_")) {
			continue
		}
		result = append(result, entry)
	}
	result = append(result, "PULSARA_TUI_BOOTSTRAP_FD="+strconv.Itoa(bootstrapFD))
	sort.Strings(result)
	return result
}
