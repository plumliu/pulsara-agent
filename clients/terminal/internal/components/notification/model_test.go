package notification

import (
	"fmt"
	"strings"
	"testing"
	"time"
)

func TestNotificationModelIsBoundedAndTerminalSafe(t *testing.T) {
	model := New()
	now := time.Date(2026, 8, 5, 12, 0, 0, 0, time.UTC)
	for index := 0; index < MaximumItems+2; index++ {
		var err error
		model, err = model.Add(Information, fmt.Sprintf("message %d\x1b]52;c;secret\a", index), fmt.Sprintf("sha256:%064d", index+1), now.Add(time.Duration(index)*time.Second), false)
		if err != nil {
			t.Fatal(err)
		}
	}
	if model.Count() != MaximumItems || model.Dropped() != 2 {
		t.Fatalf("notification bound drifted: count=%d dropped=%d", model.Count(), model.Dropped())
	}
	latest, ok := model.Latest(now.Add(9 * time.Second))
	if !ok || latest.PublicText() == "" {
		t.Fatal("latest bounded notification is unavailable")
	}
	if err := model.Validate(); err != nil {
		t.Fatal(err)
	}
}

func TestNotificationModelExpiresTransientItemsButRetainsStickyItems(t *testing.T) {
	model := New()
	now := time.Date(2026, 8, 5, 12, 0, 0, 0, time.UTC)
	var err error
	model, err = model.Add(CommandOutcome, "completed", "sha256:"+strings.Repeat("a", 64), now, false)
	if err != nil {
		t.Fatal(err)
	}
	model, err = model.Add(Warning, "sticky", "sha256:"+strings.Repeat("b", 64), now, true)
	if err != nil {
		t.Fatal(err)
	}
	model = model.Expire(now.Add(DefaultLifetime))
	if model.Count() != 1 {
		t.Fatalf("transient expiry retained the wrong item count: %d", model.Count())
	}
	latest, ok := model.Latest(now.Add(24 * time.Hour))
	if !ok || latest.PublicText() != "sticky" {
		t.Fatal("sticky notification was not retained")
	}
}

func TestNotificationExpiryUsesOneEarliestGenerationAndPreservesSeverity(t *testing.T) {
	model := New()
	now := time.Date(2026, 8, 5, 12, 0, 0, 0, time.UTC)
	var err error
	model, err = model.Add(Information, "first", "sha256:"+strings.Repeat("c", 64), now, false)
	if err != nil {
		t.Fatal(err)
	}
	model, due, generation, scheduled := model.PlanExpiry()
	if !scheduled || !due.Equal(now.Add(DefaultLifetime)) || generation == 0 {
		t.Fatal("first transient notification did not install its exact expiry")
	}
	model, err = model.Add(Information, "later", "sha256:"+strings.Repeat("d", 64), now.Add(time.Second), false)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, _, duplicate := model.PlanExpiry(); duplicate {
		t.Fatal("later notification allocated a second concurrent expiry timer")
	}
	model, applied := model.ApplyExpiryTick(generation, due)
	if !applied || model.Count() != 1 {
		t.Fatalf("expiry tick did not retire only the due notification: count=%d", model.Count())
	}
	model, _, _, scheduled = model.PlanExpiry()
	if !scheduled {
		t.Fatal("remaining transient notification did not schedule its successor expiry")
	}
	model, err = model.Add(Failure, "delivery failed", "sha256:"+strings.Repeat("e", 64), now.Add(2*time.Second), true)
	if err != nil {
		t.Fatal(err)
	}
	if rendered := RenderLatest(model); rendered != "Error · delivery failed · Esc dismiss" {
		t.Fatalf("notification severity/sticky rendering drifted: %q", rendered)
	}
	model = model.DismissLatestSticky()
	if model.LatestSticky() {
		t.Fatal("explicit sticky dismissal did not retire the latest failure")
	}
}
