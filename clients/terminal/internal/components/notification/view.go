package notification

import "fmt"

func RenderLatest(model Model) string {
	// View is intentionally clock-free. Update owns expiration; this helper only
	// renders the latest already-validated local item.
	if len(model.items) == 0 {
		return ""
	}
	item := model.items[len(model.items)-1]
	label := map[Kind]string{
		Information:    "Info",
		Warning:        "Warning",
		Failure:        "Error",
		CommandOutcome: "Command",
	}[item.kind]
	if item.sticky {
		return fmt.Sprintf("%s · %s · Esc dismiss", label, item.publicText)
	}
	return fmt.Sprintf("%s · %s", label, item.publicText)
}
