package k3keys

import (
	"bytes"
	"reflect"
	"testing"
)

func quiet(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	old := warnOut
	warnOut = &buf
	t.Cleanup(func() { warnOut = old })
	return &buf
}

// TestParseConfigFlatAndTable covers the documented flat form and the [panes] table.
func TestParseConfigFlatAndTable(t *testing.T) {
	quiet(t)
	cfg, err := parseConfig([]byte(`
"ctrl+g" = "k3:chooser"
"panes:n" = "rotate_split"

[panes]
"tabs:1" = "toggle_tiling"
`))
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]string{"ctrl+g": "k3:chooser", "panes:n": "rotate_split", "tabs:1": "toggle_tiling"}
	if !reflect.DeepEqual(cfg.Panes, want) {
		t.Fatalf("Panes = %v, want %v", cfg.Panes, want)
	}
}

func TestParseConfigSyntaxErrorIsReported(t *testing.T) {
	if _, err := parseConfig([]byte(`"ctrl+g" = `)); err == nil {
		t.Fatal("want a parse error")
	}
}

// TestUnknownModePrefixIsSkipped: a typo like "pane:n" must not bind "n" in typing mode.
func TestUnknownModePrefixIsSkipped(t *testing.T) {
	buf := quiet(t)
	got := ParseUserBindings(Config{Panes: map[string]string{"pane:n": "new_window", "Panes:X": "close_window"}})
	if _, ok := got[ModeTyping]["n"]; ok {
		t.Fatal("unknown prefix bound a typing-mode key")
	}
	if got[ModePanes]["x"] != "close_window" {
		t.Fatalf("mixed-case key not lowercased: %v", got)
	}
	if buf.Len() == 0 {
		t.Fatal("unknown mode not reported")
	}
	s := NewKeyState()
	if acts, consumed := s.Handle("n", MergeBindings(got)); consumed || acts != nil {
		t.Fatalf("typing 'n' = %v, %v; want pass-through", acts, consumed)
	}
}

// TestUserOverrideOfGlobalKey: overriding a global key applies in typing and leader modes.
func TestUserOverrideOfGlobalKey(t *testing.T) {
	b := MergeBindings(map[Mode]map[string]string{ModeTyping: {"alt+n": "toggle_zoom"}})
	for _, mode := range []Mode{ModeTyping, ModePanes} {
		s := NewKeyState()
		s.Mode = mode
		acts, consumed := s.Handle("alt+n", b)
		if !consumed || !reflect.DeepEqual(acts, []string{"toggle_zoom"}) {
			t.Errorf("%v: alt+n = %v, %v; want [toggle_zoom]", mode, acts, consumed)
		}
	}
}
