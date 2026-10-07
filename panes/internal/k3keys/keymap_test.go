package k3keys

import (
	"reflect"
	"testing"
)

// step is one key press in a scenario: the key sent, the actions expected back,
// and whether the key was consumed.
type step struct {
	key      string
	actions  []string
	consumed bool
}

// run drives a fresh KeyState through steps, failing at the first mismatch and
// reporting the mode the state was left in.
func run(t *testing.T, name string, steps []step) {
	t.Helper()
	t.Run(name, func(t *testing.T) {
		s := NewKeyState()
		bindings := MergeBindings(nil)
		for i, st := range steps {
			actions, consumed := s.Handle(st.key, bindings)
			if consumed != st.consumed {
				t.Fatalf("step %d (%q): consumed = %v, want %v (mode now %v)", i, st.key, consumed, st.consumed, s.Mode)
			}
			if !reflect.DeepEqual(actions, st.actions) {
				t.Fatalf("step %d (%q): actions = %v, want %v (mode now %v)", i, st.key, actions, st.actions, s.Mode)
			}
		}
	})
}

// TestLeaderToModeToActionToTyping covers the full round trip: the leader opens
// the Chooser, a letter selects a mode, a one-shot action runs and the state is
// back in Typing with enter_terminal_mode appended so keys reach the pane.
func TestLeaderToModeToActionToTyping(t *testing.T) {
	run(t, "panes: split_vertical", []step{
		{"ctrl+g", nil, true},
		{"p", nil, true},
		{"v", []string{"split_vertical", "enter_terminal_mode"}, true},
	})
	if NewKeyState().Mode != ModeTyping {
		t.Fatal("fresh state is not in Typing mode")
	}
	run(t, "panes: action then state is back in Typing", []step{
		{"ctrl+g", nil, true},
		{"p", nil, true},
		{"v", []string{"split_vertical", "enter_terminal_mode"}, true},
	})
	run(t, "tabs: next workspace", []step{
		{"ctrl+g", nil, true},
		{"t", nil, true},
		{"n", []string{"next_workspace", "enter_terminal_mode"}, true},
	})
	run(t, "sessions: new session", []step{
		{"ctrl+g", nil, true},
		{"s", nil, true},
		{"n", []string{"new_session", "enter_terminal_mode"}, true},
	})
}

// TestLockViaRepeatedLetter covers locking: pressing a mode's own letter again
// pins the mode, and a one-shot action inside a locked mode leaves the mode.
func TestLockViaRepeatedLetter(t *testing.T) {
	s := NewKeyState()
	bindings := MergeBindings(nil)
	for _, key := range []string{"ctrl+g", "p", "p"} {
		s.Handle(key, bindings)
	}
	if s.Mode != ModePanes || !s.Locked {
		t.Fatalf("after ctrl+g p p: mode = %v, locked = %v; want Panes locked", s.Mode, s.Locked)
	}
	// A one-shot action in a locked mode runs but does not leave the mode.
	actions, consumed := s.Handle("v", bindings)
	if !consumed || !reflect.DeepEqual(actions, []string{"split_vertical"}) {
		t.Fatalf("locked panes v: actions = %v, consumed = %v", actions, consumed)
	}
	if s.Mode != ModePanes || !s.Locked {
		t.Fatalf("locked mode did not hold: mode = %v, locked = %v", s.Mode, s.Locked)
	}
	// Esc still gets out.
	actions, consumed = s.Handle("esc", bindings)
	if !consumed || !reflect.DeepEqual(actions, []string{"enter_terminal_mode"}) {
		t.Fatalf("esc from locked panes: actions = %v, consumed = %v", actions, consumed)
	}
	if s.Mode != ModeTyping || s.Locked {
		t.Fatalf("esc from locked panes: mode = %v, locked = %v; want Typing unlocked", s.Mode, s.Locked)
	}
}

// TestResizeLockByDefault covers the one mode that is locked as soon as it is
// entered, and stays locked through actions until Esc.
func TestResizeLockByDefault(t *testing.T) {
	s := NewKeyState()
	bindings := MergeBindings(nil)
	for _, key := range []string{"ctrl+g", "r"} {
		s.Handle(key, bindings)
	}
	if s.Mode != ModeResize || !s.Locked {
		t.Fatalf("after ctrl+g r: mode = %v, locked = %v; want Resize locked", s.Mode, s.Locked)
	}
	actions, _ := s.Handle("right", bindings)
	if !reflect.DeepEqual(actions, []string{"resize_height_grow"}) {
		t.Fatalf("resize right: actions = %v", actions)
	}
	if s.Mode != ModeResize || !s.Locked {
		t.Fatalf("resize action left the mode: mode = %v, locked = %v", s.Mode, s.Locked)
	}
}

// TestEscFromEveryMode covers Esc returning to Typing from every mode, always
// with enter_terminal_mode so keys reach the pane again.
func TestEscFromEveryMode(t *testing.T) {
	for _, m := range []Mode{ModeChooser, ModePanes, ModeTabs, ModeSessions, ModeResize, ModeSearch, ModeAgents} {
		name := "esc from " + m.String()
		t.Run(name, func(t *testing.T) {
			s := NewKeyState()
			bindings := MergeBindings(nil)
			s.Mode = m
			s.Locked = true // even from a locked state
			actions, consumed := s.Handle("esc", bindings)
			if !consumed {
				t.Fatalf("esc not consumed in %v", m)
			}
			if !reflect.DeepEqual(actions, []string{"enter_terminal_mode"}) {
				t.Fatalf("esc from %v: actions = %v", m, actions)
			}
			if s.Mode != ModeTyping || s.Locked {
				t.Fatalf("esc from %v: mode = %v, locked = %v", m, s.Mode, s.Locked)
			}
		})
	}
}

// TestPassthroughInTyping covers Typing mode consuming nothing it does not
// bind: every other key goes to HandleInput untouched.
func TestPassthroughInTyping(t *testing.T) {
	for _, key := range []string{"a", "x", "?", "enter", "ctrl+c", "space", "/", "p"} {
		s := NewKeyState()
		actions, consumed := s.Handle(key, MergeBindings(nil))
		if consumed {
			t.Errorf("key %q consumed in Typing mode", key)
		}
		if actions != nil {
			t.Errorf("key %q returned actions %v in Typing mode", key, actions)
		}
	}
}

// TestGlobalAltKeysInEveryMode covers the global bindings working in all modes,
// including while a mode is locked.
func TestGlobalAltKeysInEveryMode(t *testing.T) {
	globals := map[string]string{
		"alt+left":  "terminal_focus_left",
		"alt+right": "terminal_focus_right",
		"alt+up":    "terminal_focus_up",
		"alt+down":  "terminal_focus_down",
		"alt+n":     "new_window",
		"alt+1":     "switch_workspace_1",
		"alt+9":     "switch_workspace_9",
		"alt+z":     "toggle_zoom",
		"ctrl+p":    "command_palette",
	}
	for _, mode := range []Mode{ModeTyping, ModeChooser, ModePanes, ModeTabs, ModeSessions, ModeResize, ModeSearch, ModeAgents} {
		for key, want := range globals {
			s := NewKeyState()
			s.Mode = mode
			s.Locked = true
			actions, consumed := s.Handle(key, MergeBindings(nil))
			if !consumed {
				t.Errorf("%v: %q not consumed", mode, key)
			}
			if !reflect.DeepEqual(actions, []string{want}) {
				t.Errorf("%v: %q actions = %v, want [%s]", mode, key, actions, want)
			}
		}
	}
}

// TestChooserSelectsEveryMode covers the Chooser routing to all six modes.
func TestChooserSelectsEveryMode(t *testing.T) {
	cases := map[string]Mode{
		"p": ModePanes,
		"t": ModeTabs,
		"s": ModeSessions,
		"r": ModeResize,
		"/": ModeSearch,
		"a": ModeAgents,
	}
	for key, want := range cases {
		s := NewKeyState()
		bindings := MergeBindings(nil)
		s.Handle("ctrl+g", bindings)
		s.Handle(key, bindings)
		if s.Mode != want {
			t.Errorf("chooser %q: mode = %v, want %v", key, s.Mode, want)
		}
	}
}

// TestHintsPerMode covers the hint tables: one essential esc hint in every
// non-typing mode, a non-empty strip everywhere, and the lock suffix.
func TestHintsPerMode(t *testing.T) {
	for _, mode := range []Mode{ModeTyping, ModeChooser, ModePanes, ModeTabs, ModeSessions, ModeResize, ModeSearch, ModeAgents} {
		hints := Hints(mode, false)
		if len(hints) == 0 {
			t.Errorf("%v: no hints", mode)
			continue
		}
		for _, h := range hints {
			if h.Key == "" || h.Label == "" {
				t.Errorf("%v: hint with empty key or label: %+v", mode, h)
			}
		}
	}
	// Esc is essential in every non-typing mode.
	for _, mode := range []Mode{ModeChooser, ModePanes, ModeTabs, ModeSessions, ModeResize, ModeSearch, ModeAgents} {
		found := false
		for _, h := range Hints(mode, false) {
			if h.Key == "esc" && h.Priority == HintEssential {
				found = true
			}
		}
		if !found {
			t.Errorf("%v: no essential esc hint", mode)
		}
	}
	// The lock suffix appears when locked and not otherwise.
	unlocked := Hints(ModePanes, false)
	locked := Hints(ModePanes, true)
	if reflect.DeepEqual(unlocked, locked) {
		t.Error("locked and unlocked hints are identical")
	}
	if len(unlocked) != len(locked) {
		t.Fatalf("locked hints changed count: %d -> %d", len(unlocked), len(locked))
	}
	for i := range locked {
		if locked[i].Label == unlocked[i].Label {
			t.Errorf("hint %d label unchanged under lock: %q", i, locked[i].Label)
		}
	}
	// The chooser names all six modes plus help and esc.
	chooser := Hints(ModeChooser, false)
	if len(chooser) != 8 { // 6 modes + ? + esc
		t.Errorf("chooser hints: got %d, want 8", len(chooser))
	}
}

// TestModeLabel covers the label switch, including the zero value.
func TestModeLabel(t *testing.T) {
	if got := ModeLabel(ModeTyping); got != "TYPING" {
		t.Errorf("ModeLabel(Typing) = %q", got)
	}
	if got := ModeLabel(Mode(99)); got != "UNKNOWN" {
		t.Errorf("ModeLabel(unknown) = %q", got)
	}
}

// TestConfigOverride covers user bindings taking precedence over defaults, and
// the mode:key prefix parsing.
func TestConfigOverride(t *testing.T) {
	user := map[string]string{
		"n":         "close_window",  // typing mode: unbindable override
		"panes:n":   "rotate_split",  // panes mode: n re-bound
		"panes:q":   "close_window",  // panes mode: new binding
		"tabs:1":    "toggle_tiling", // tabs mode: digit re-bound
		"chooser:z": "k3:mode:resize",
	}
	parsed := ParseUserBindings(Config{Panes: user})
	bindings := MergeBindings(parsed)

	// Panes: n now runs the user's action.
	s := NewKeyState()
	for _, key := range []string{"ctrl+g", "p"} {
		s.Handle(key, bindings)
	}
	actions, _ := s.Handle("n", bindings)
	if !reflect.DeepEqual(actions, []string{"rotate_split", "enter_terminal_mode"}) {
		t.Errorf("overridden panes n: actions = %v", actions)
	}
	// The new binding works too.
	s = NewKeyState()
	for _, key := range []string{"ctrl+g", "p"} {
		s.Handle(key, bindings)
	}
	actions, _ = s.Handle("q", bindings)
	if !reflect.DeepEqual(actions, []string{"close_window", "enter_terminal_mode"}) {
		t.Errorf("added panes q: actions = %v", actions)
	}
	// Defaults the user did not mention survive.
	s = NewKeyState()
	for _, key := range []string{"ctrl+g", "p"} {
		s.Handle(key, bindings)
	}
	actions, _ = s.Handle("=", bindings)
	if !reflect.DeepEqual(actions, []string{"equalize_splits", "enter_terminal_mode"}) {
		t.Errorf("default panes =: actions = %v", actions)
	}
	// Typing override is picked up before the passthrough check.
	s = NewKeyState()
	actions, consumed := s.Handle("n", bindings)
	if !consumed || !reflect.DeepEqual(actions, []string{"close_window"}) {
		t.Errorf("overridden typing n: actions = %v, consumed = %v", actions, consumed)
	}
}

// TestMergeBindingsNilSafe covers merging with no user bindings at all.
func TestMergeBindingsNilSafe(t *testing.T) {
	bindings := MergeBindings(nil)
	if len(bindings[ModePanes]) == 0 {
		t.Fatal("merged bindings lost the defaults")
	}
	// The merge must copy, not alias: mutating the result must not touch the
	// defaults for the next caller.
	bindings[ModePanes]["n"] = "clobbered"
	fresh := MergeBindings(nil)
	if fresh[ModePanes]["n"] != "new_window" {
		t.Fatal("MergeBindings aliases defaultBindings")
	}
}
