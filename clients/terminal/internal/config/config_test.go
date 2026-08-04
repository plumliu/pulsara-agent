package config

import (
	"slices"
	"testing"
)

func TestSanitizedEnvironmentExcludesCredentials(t *testing.T) {
	value := SanitizedEnvironment([]string{
		"TERM=xterm-256color",
		"LANG=zh_CN.UTF-8",
		"LC_CTYPE=UTF-8",
		"PULSARA_API_KEY=secret",
		"HOME=/private/home",
	}, 9)
	for _, expected := range []string{
		"LANG=zh_CN.UTF-8",
		"LC_CTYPE=UTF-8",
		"PULSARA_TUI_BOOTSTRAP_FD=9",
		"TERM=xterm-256color",
	} {
		if !slices.Contains(value, expected) {
			t.Fatalf("missing environment value %q", expected)
		}
	}
	if slices.Contains(value, "PULSARA_API_KEY=secret") || slices.Contains(value, "HOME=/private/home") {
		t.Fatal("secret or unrelated environment escaped into the client")
	}
}
