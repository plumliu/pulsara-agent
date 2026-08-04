package app

import (
	"errors"
	"reflect"
	"unicode/utf8"

	tea "charm.land/bubbletea/v2"
)

// Bubble Tea handles SetClipboard's private message before forwarding that
// same message to Model.Update. Keep the framework-private carrier out of the
// application vocabulary without inspecting or retaining its clipboard text.
// The exact runtime type is obtained from the pinned public constructor rather
// than duplicated by package/name strings.
var bubbleTeaSetClipboardMessageType = reflect.TypeOf(tea.SetClipboard("")())

type KeyAction uint16

const (
	KeyText KeyAction = iota + 1
	KeyEnter
	KeyBackspace
	KeyDelete
	KeyLeft
	KeyRight
	KeyUp
	KeyDown
	KeyHome
	KeyEnd
	KeyPageUp
	KeyPageDown
	KeyTab
	KeyBackTab
	KeyEscape
	KeyInterrupt
	KeyEOF
)

type KeyModifiers uint8

const (
	KeyModShift KeyModifiers = 1 << iota
	KeyModAlt
	KeyModCtrl
)

type NormalizedKey struct {
	Action    KeyAction
	Modifiers KeyModifiers
	TextUTF8  string
	Repeat    bool
}

func NewNormalizedKey(action KeyAction, modifiers KeyModifiers, text string, repeat bool) (NormalizedKey, error) {
	if action < KeyText || action > KeyEOF || modifiers&^(KeyModShift|KeyModAlt|KeyModCtrl) != 0 || !utf8.ValidString(text) || len([]byte(text)) > 256 {
		return NormalizedKey{}, errors.New("terminal normalized key is invalid")
	}
	if (action == KeyText) != (text != "") {
		return NormalizedKey{}, errors.New("terminal normalized key text matrix is invalid")
	}
	return NormalizedKey{Action: action, Modifiers: modifiers, TextUTF8: text, Repeat: repeat}, nil
}

type PasteBoundary uint8

const (
	PasteStarted PasteBoundary = iota + 1
	PasteCompleted
	PasteCancelled
)

type MouseWheelDirection uint8

const (
	MouseWheelScrollUp MouseWheelDirection = iota + 1
	MouseWheelScrollDown
)

const mouseWheelVisualRows uint8 = 3

func normalizeFrameworkMessage(
	message tea.Msg,
	header LocalMessageHeader,
) (any, bool) {
	switch value := message.(type) {
	case tea.WindowSizeMsg:
		return ResizeMsg{Header: header, Width: value.Width, Height: value.Height}, true
	case tea.FocusMsg:
		return FocusChangedMsg{Header: header, Focused: true}, true
	case tea.BlurMsg:
		return FocusChangedMsg{Header: header, Focused: false}, true
	case tea.KeyPressMsg:
		keyValue := value.Key()
		action, text := KeyText, keyValue.Text
		switch value.Keystroke() {
		case "enter":
			action, text = KeyEnter, ""
		case "backspace":
			action, text = KeyBackspace, ""
		case "delete":
			action, text = KeyDelete, ""
		case "left":
			action, text = KeyLeft, ""
		case "right":
			action, text = KeyRight, ""
		case "up":
			action, text = KeyUp, ""
		case "down":
			action, text = KeyDown, ""
		case "home":
			action, text = KeyHome, ""
		case "end":
			action, text = KeyEnd, ""
		case "pgup":
			action, text = KeyPageUp, ""
		case "pgdown":
			action, text = KeyPageDown, ""
		case "tab":
			action, text = KeyTab, ""
		case "shift+tab":
			action, text = KeyBackTab, ""
		case "esc":
			action, text = KeyEscape, ""
		case "ctrl+c":
			action, text = KeyInterrupt, ""
		case "ctrl+d":
			action, text = KeyEOF, ""
		}
		modifiers := KeyModifiers(0)
		if keyValue.Mod&tea.ModShift != 0 {
			modifiers |= KeyModShift
		}
		if keyValue.Mod&tea.ModAlt != 0 {
			modifiers |= KeyModAlt
		}
		if keyValue.Mod&tea.ModCtrl != 0 {
			modifiers |= KeyModCtrl
		}
		key, err := NewNormalizedKey(action, modifiers, text, keyValue.IsRepeat)
		if err != nil {
			return FrameworkInputRejectedMsg{Header: header}, true
		}
		return KeyInputMsg{Header: header, Key: key}, true
	case tea.PasteStartMsg:
		return PasteBoundaryMsg{Header: header, Boundary: PasteStarted}, true
	case tea.PasteMsg:
		if !utf8.ValidString(value.Content) || len([]byte(value.Content)) > 1024*1024 {
			return FrameworkInputRejectedMsg{Header: header}, true
		}
		return PasteInputMsg{Header: header, ChunkUTF8: value.Content, ByteCount: uint32(len([]byte(value.Content)))}, true
	case tea.PasteEndMsg:
		return PasteBoundaryMsg{Header: header, Boundary: PasteCompleted}, true
	case tea.MouseWheelMsg:
		switch value.Mouse().Button {
		case tea.MouseWheelUp:
			return MouseWheelInputMsg{Header: header, Direction: MouseWheelScrollUp, VisualRows: mouseWheelVisualRows}, true
		case tea.MouseWheelDown:
			return MouseWheelInputMsg{Header: header, Direction: MouseWheelScrollDown, VisualRows: mouseWheelVisualRows}, true
		default:
			return FrameworkAdvisoryIgnoredMsg{Header: header, Kind: FrameworkAdvisoryMousePointer}, true
		}
	case tea.MouseClickMsg, tea.MouseReleaseMsg, tea.MouseMotionMsg:
		return FrameworkAdvisoryIgnoredMsg{Header: header, Kind: FrameworkAdvisoryMousePointer}, true
	case tea.KeyboardEnhancementsMsg:
		return KeyboardEnhancementsObservedMsg{Header: header, Flags: value.Flags}, true
	case tea.EnvMsg:
		return FrameworkAdvisoryIgnoredMsg{Header: header, Kind: FrameworkAdvisoryEnvironment}, true
	case tea.ColorProfileMsg:
		return FrameworkAdvisoryIgnoredMsg{Header: header, Kind: FrameworkAdvisoryColorProfile}, true
	case tea.KeyReleaseMsg:
		return FrameworkAdvisoryIgnoredMsg{Header: header, Kind: FrameworkAdvisoryKeyRelease}, true
	case tea.CursorPositionMsg:
		return FrameworkAdvisoryIgnoredMsg{Header: header, Kind: FrameworkAdvisoryCursorPosition}, true
	case tea.TerminalVersionMsg:
		return FrameworkAdvisoryIgnoredMsg{Header: header, Kind: FrameworkAdvisoryTerminalVersion}, true
	case tea.CapabilityMsg:
		return FrameworkAdvisoryIgnoredMsg{Header: header, Kind: FrameworkAdvisoryCapability}, true
	case tea.ForegroundColorMsg, tea.BackgroundColorMsg, tea.CursorColorMsg:
		return FrameworkAdvisoryIgnoredMsg{Header: header, Kind: FrameworkAdvisoryColorReport}, true
	case tea.ModeReportMsg:
		return FrameworkAdvisoryIgnoredMsg{Header: header, Kind: FrameworkAdvisoryModeReport}, true
	case tea.ClipboardMsg:
		return FrameworkAdvisoryIgnoredMsg{Header: header, Kind: FrameworkAdvisoryClipboard}, true
	default:
		if reflect.TypeOf(message) == bubbleTeaSetClipboardMessageType {
			return FrameworkAdvisoryIgnoredMsg{Header: header, Kind: FrameworkAdvisoryClipboard}, true
		}
		return message, false
	}
}
