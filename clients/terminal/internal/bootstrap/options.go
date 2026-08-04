package bootstrap

import "time"

const (
	MaximumCarrierBytes = 16 * 1024
	ReadDeadline        = 2 * time.Second
)

type Options struct {
	FD  int
	Now func() time.Time
}

func (o Options) withDefaults() Options {
	if o.FD < 0 {
		o.FD = 3
	}
	if o.Now == nil {
		o.Now = time.Now
	}
	return o
}
