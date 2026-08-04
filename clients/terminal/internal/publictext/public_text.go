// Package publictext owns the renderer-safe transform for untrusted text.
package publictext

import (
	"strings"
	"unicode/utf8"
)

const upperHex = "0123456789ABCDEF"

// Transform replaces terminal control code points with inert visible escapes.
// Horizontal tab and line feed are the only admitted C0 controls.
func Transform(value string) string {
	var result strings.Builder
	result.Grow(len(value))
	for len(value) != 0 {
		character, width := utf8.DecodeRuneInString(value)
		if character == utf8.RuneError && width == 1 {
			// Go strings may contain arbitrary bytes.  An invalid UTF-8 byte must
			// never be normalized to U+FFFD because that would hide a C1 control
			// byte from this security boundary.
			result.WriteString(`\x`)
			result.WriteByte(upperHex[value[0]>>4])
			result.WriteByte(upperHex[value[0]&0xF])
			value = value[1:]
			continue
		}
		value = value[width:]
		codePoint := uint32(character)
		if character == '\t' || character == '\n' {
			result.WriteRune(character)
			continue
		}
		if codePoint < 0x20 || (codePoint >= 0x7F && codePoint <= 0x9F) {
			result.WriteString(`\x`)
			result.WriteByte(upperHex[(codePoint>>4)&0xF])
			result.WriteByte(upperHex[codePoint&0xF])
			continue
		}
		result.WriteRune(character)
	}
	return result.String()
}

// IsSafe reports whether Transform would leave the value unchanged.
func IsSafe(value string) bool { return Transform(value) == value }

// Bounded transforms and then truncates without splitting a rune.
func Bounded(value string, maximumRunes, maximumBytes int) string {
	if maximumRunes < 1 || maximumBytes < 1 {
		return ""
	}
	safe := Transform(value)
	var result strings.Builder
	count := 0
	for _, character := range safe {
		if count >= maximumRunes {
			break
		}
		encoded := string(character)
		if result.Len()+len(encoded) > maximumBytes {
			break
		}
		result.WriteString(encoded)
		count++
	}
	return result.String()
}
