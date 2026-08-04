package transcript

// Scrolling transitions intentionally live on Model in model.go so there is a
// single constructor/validator boundary for viewport state. This file remains
// the final component update owner for the S2 observation slice.
