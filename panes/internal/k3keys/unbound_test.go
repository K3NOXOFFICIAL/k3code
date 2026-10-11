package k3keys

import (
	"bytes"
	"reflect"
	"strings"
	"testing"
)

// TestEmptyActionUnbindsTheKey: `"alt+n" = ""` is how a user removes a binding.
// Typing mode used to run the empty action and swallow the key; the key must
// reach the pane, exactly as it does for a key that was never bound.
func TestEmptyActionUnbindsTheKey(t *testing.T) {
	bindings := MergeBindings(ParseUserBindings(Config{Panes: map[string]string{"alt+n": ""}}))
	s := NewKeyState()
	actions, consumed := s.Handle("alt+n", bindings)
	if consumed || actions != nil {
		t.Fatalf("typing alt+n = %q: Handle = %v, %v; want it passed through ([] false)", "", actions, consumed)
	}
	// In a leader mode the global fallback does not apply to a key the user
	// emptied, and the key does nothing instead of running the empty action.
	s.Mode = ModePanes
	actions, consumed = s.Handle("alt+n", bindings)
	if !consumed || len(actions) != 0 {
		t.Fatalf("panes alt+n = %q: Handle = %v, %v; want [] true", "", actions, consumed)
	}
}

// TestEmptyActionInALeaderModeDoesNothing: an emptied mode key is consumed with
// no action and leaves the mode as it was; it must not run "" or fall back to
// Typing the way a real one-shot action does.
func TestEmptyActionInALeaderModeDoesNothing(t *testing.T) {
	bindings := MergeBindings(ParseUserBindings(Config{Panes: map[string]string{"panes:v": ""}}))
	s := NewKeyState()
	s.Handle("ctrl+g", bindings)
	s.Handle("p", bindings)
	actions, consumed := s.Handle("v", bindings)
	if !consumed || len(actions) != 0 {
		t.Fatalf("panes v = %q: Handle = %v, %v; want [] true", "", actions, consumed)
	}
	if s.Mode != ModePanes {
		t.Fatalf("mode after an emptied key = %v, want Panes", s.Mode)
	}
	// A neighbouring key still works.
	actions, _ = s.Handle("x", bindings)
	if !reflect.DeepEqual(actions, []string{"close_window", "enter_terminal_mode"}) {
		t.Fatalf("panes x after the emptied v: %v", actions)
	}
}

func TestUnknownActionsAreReported(t *testing.T) {
	var buf bytes.Buffer
	old := warnOut
	warnOut = &buf
	t.Cleanup(func() { warnOut = old })

	// A dispatcher that knows every default action, so only the user's typos
	// are left to report.
	known := map[string]bool{}
	for _, binds := range defaultBindings {
		for _, action := range binds {
			known[action] = true
		}
	}
	bindings := MergeBindings(ParseUserBindings(Config{Panes: map[string]string{
		"alt+n":     "new_windwo",     // a typo, in Typing mode
		"panes:q":   "close_window",   // fine
		"tabs:z":    "",               // unbound on purpose: not a problem
		"panes:esc": "k3:typing",      // a k3 pseudo-action
		"panes:t":   "k3:mode:tabz",   // a misspelled pseudo-action
		"panes:w":   "closs_window",   // a second typo, in a leader mode
		"chooser:p": "k3:mode:panes",  // fine
		"panes:y":   "esc",            // fine
		"panes:u":   "k3:lock",        // fine
		"agents:a":  "k3:chooser",     // fine
		"search:i":  "k3:mode:search", // fine
	}}))
	warnUnknownActions(bindings, func(a string) bool { return known[a] })
	lines := strings.Split(strings.TrimSpace(buf.String()), "\n")
	if len(lines) != 3 {
		t.Fatalf("want 3 warnings (the three typos), got %d:\n%s", len(lines), buf.String())
	}
	for _, line := range lines {
		if !strings.HasPrefix(line, "k3: keys.toml: ") {
			t.Errorf("warning does not follow the keys.toml format: %q", line)
		}
	}
	for _, want := range []string{`"new_windwo"`, `"k3:mode:tabz"`, `"closs_window"`} {
		if !strings.Contains(buf.String(), want) {
			t.Errorf("no warning for %s:\n%s", want, buf.String())
		}
	}
}

// TestDefaultBindingsNameActionsTheDispatcherRuns: the startup warning must be
// silent for the defaults, or every user would see it on every start.
func TestDefaultBindingsNameActionsTheDispatcherRuns(t *testing.T) {
	var buf bytes.Buffer
	old := warnOut
	warnOut = &buf
	t.Cleanup(func() { warnOut = old })
	warnUnknownActions(MergeBindings(nil), actionKnown)
	if buf.Len() != 0 {
		t.Fatalf("a default binding names an action nothing runs:\n%s", buf.String())
	}
}
