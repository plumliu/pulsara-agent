package commandstate

import "errors"

const S1MaximumRecords uint32 = 64

type Registry struct {
	maximum    uint32
	generation uint64
	enabled    bool
	records    []Record
}

type Record struct {
	commandID                  string
	requestSemanticFingerprint string
	phase                      uint8
}

func NewDormantRegistry(maximum uint32) (Registry, error) {
	if maximum != S1MaximumRecords {
		return Registry{}, errors.New("terminal command registry bound is incompatible")
	}
	return Registry{maximum: maximum, generation: 1}, nil
}

func (r Registry) Validate() error {
	if r.maximum != S1MaximumRecords || r.generation == 0 || uint32(len(r.records)) > r.maximum {
		return errors.New("terminal command registry is invalid")
	}
	if !r.enabled && len(r.records) != 0 {
		return errors.New("dormant command registry contains records")
	}
	return nil
}
func (r Registry) Dormant() bool { return !r.enabled }
func (r Registry) Count() int    { return len(r.records) }
