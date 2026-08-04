package secret

type Handle struct{ id string }

func (h Handle) ID() string { return h.id }
