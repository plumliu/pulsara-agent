package buildinfo

import (
	"errors"
	"regexp"
	"runtime"
	"strings"

	protocolv3 "github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolv3"
)

var (
	Version                          = "0.0.0-dev"
	Commit                           = "unknown"
	ProtocolMajor             uint32 = 3
	ProtocolMinor             uint32 = 0
	SchemaFingerprint                = "sha256:c8571a6124c4b02f6d4b10911fbd11aa46517f05b84408b6606fa8c85866dbbe"
	DependencyLockFingerprint        = "development"
)

var semanticVersion = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$`)

type BuildIdentity struct {
	productVersion            string
	sourceRevision            string
	goVersion                 string
	targetOS                  string
	targetArch                string
	protocolSchemaFingerprint string
	buildFingerprint          string
}

func Current() (BuildIdentity, error) {
	return NewBuildIdentity(
		Version,
		Commit,
		runtime.Version(),
		runtime.GOOS,
		runtime.GOARCH,
		SchemaFingerprint,
	)
}

func NewBuildIdentity(
	productVersion string,
	sourceRevision string,
	goVersion string,
	targetOS string,
	targetArch string,
	protocolSchemaFingerprint string,
) (BuildIdentity, error) {
	value := BuildIdentity{
		productVersion:            productVersion,
		sourceRevision:            sourceRevision,
		goVersion:                 goVersion,
		targetOS:                  targetOS,
		targetArch:                targetArch,
		protocolSchemaFingerprint: protocolSchemaFingerprint,
	}
	fingerprint, err := value.expectedFingerprint()
	if err != nil {
		return BuildIdentity{}, err
	}
	value.buildFingerprint = fingerprint
	if err := value.Validate(); err != nil {
		return BuildIdentity{}, err
	}
	return value, nil
}

func (v BuildIdentity) Validate() error {
	if !semanticVersion.MatchString(v.productVersion) || v.sourceRevision == "" ||
		!strings.HasPrefix(v.goVersion, "go1.") ||
		(v.targetOS != "darwin" && v.targetOS != "linux") ||
		(v.targetArch != "amd64" && v.targetArch != "arm64") ||
		!strings.HasPrefix(v.protocolSchemaFingerprint, "sha256:") ||
		len(v.protocolSchemaFingerprint) != len("sha256:")+64 ||
		v.buildFingerprint == "" {
		return errors.New("terminal build identity is invalid")
	}
	expected, err := v.expectedFingerprint()
	if err != nil || expected != v.buildFingerprint {
		return errors.New("terminal build identity fingerprint mismatch")
	}
	return nil
}

func (v BuildIdentity) expectedFingerprint() (string, error) {
	return protocolv3.CanonicalFingerprint(
		"terminal-client-build-identity:v1",
		map[string]any{
			"product_version":             v.productVersion,
			"source_revision":             v.sourceRevision,
			"go_version":                  v.goVersion,
			"target_os":                   v.targetOS,
			"target_arch":                 v.targetArch,
			"protocol_schema_fingerprint": v.protocolSchemaFingerprint,
		},
	)
}

func (v BuildIdentity) Fingerprint() string { return v.buildFingerprint }
