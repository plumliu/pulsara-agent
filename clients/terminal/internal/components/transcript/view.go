package transcript

// Rendering is a pure projection of rows prepared by Model. App owns the
// final full-height composition and line padding.
func Render(model Model) []string { return model.RenderLines() }
