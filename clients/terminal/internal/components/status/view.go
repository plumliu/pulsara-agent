package status

import "fmt"

func Durable(revision uint64, queueItems int, unseen uint32, stale bool) string {
	value := fmt.Sprintf("revision %d · queue %d", revision, queueItems)
	if unseen > 0 {
		value += fmt.Sprintf(" · %d new", unseen)
	}
	if stale {
		value += " · stale"
	}
	return value
}
