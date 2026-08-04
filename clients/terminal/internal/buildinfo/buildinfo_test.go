package buildinfo

import "testing"

func TestBuildIdentityIsClosedAndFingerprintStable(t *testing.T) {
	value, err := NewBuildIdentity(
		"1.2.3-test.1",
		"revision:one",
		"go1.26.5",
		"darwin",
		"arm64",
		"sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
	)
	if err != nil {
		t.Fatal(err)
	}
	if value.Fingerprint() == "" || value.Validate() != nil {
		t.Fatal("validated build identity lost its proof")
	}
	if _, err := NewBuildIdentity("dev", "revision:one", "go1.26.5", "darwin", "arm64", "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"); err == nil {
		t.Fatal("non-semver build identity was accepted")
	}
	if _, err := NewBuildIdentity("1.2.3", "revision:one", "go1.26.5", "windows", "arm64", "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"); err == nil {
		t.Fatal("unsupported terminal target was accepted")
	}
}
