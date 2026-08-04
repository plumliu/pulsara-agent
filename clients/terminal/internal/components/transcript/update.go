package transcript

func (m Model) Scroll(delta int) Model { m.state = m.state.Scroll(delta); return m }
