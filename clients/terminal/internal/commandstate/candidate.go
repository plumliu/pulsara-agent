package commandstate

type Candidate struct{ id string }

func (c Candidate) ID() string { return c.id }
