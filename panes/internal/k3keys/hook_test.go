package k3keys

import (
	"testing"

	tea "charm.land/bubbletea/v2"

	"github.com/Gaurav-Gosain/tuios/internal/app"
	"github.com/Gaurav-Gosain/tuios/internal/config"
	"github.com/Gaurav-Gosain/tuios/internal/input"
)

// installForTest wires the k3 hooks into tuios for one test and takes them out
// again: both are package-level variables every other test of the binary sees.
func installForTest(t *testing.T) (*KeyState, *app.OS) {
	t.Helper()
	state := NewKeyState()
	Install(state, MergeBindings(nil))
	t.Cleanup(func() {
		input.PreHandler = nil
		app.LegendOverride = nil
	})
	return state, &app.OS{Settings: config.Global, Mode: app.TerminalMode, RemoteClient: true}
}

func press(o *app.OS, keys ...tea.KeyPressMsg) {
	for _, k := range keys {
		input.HandleInput(k, o)
	}
}

var (
	ctrlG    = tea.KeyPressMsg{Code: 'g', Mod: tea.ModCtrl}
	question = tea.KeyPressMsg{Code: '?', Text: "?"}
	slash    = tea.KeyPressMsg{Code: '/', Text: "/"}
	esc      = tea.KeyPressMsg{Code: tea.KeyEscape}
)

// TestEscClosesTheHelpOpenedFromTheChooser is #170's "k3 Esc overlay": ctrl+g ?
// opens the help overlay and leaves the keymap in the chooser, which took every
// key first. Esc only switched the keymap back to typing and the help stayed up
// until a second Esc; its own keys (/ to search) switched k3 modes instead.
func TestEscClosesTheHelpOpenedFromTheChooser(t *testing.T) {
	state, o := installForTest(t)

	press(o, ctrlG, question)
	if !o.ShowHelp {
		t.Fatal("ctrl+g ? did not open the help overlay")
	}

	press(o, esc)
	if o.ShowHelp {
		t.Fatal("esc left the help overlay open")
	}
	if state.Mode != ModeTyping {
		t.Fatalf("after esc the keymap is in %v, want Typing", state.Mode)
	}
}

func TestAnOpenOverlayGetsItsOwnKeys(t *testing.T) {
	state, o := installForTest(t)

	press(o, ctrlG, question, slash)
	if !o.HelpSearchMode {
		t.Fatal("/ in the help overlay did not start its search")
	}
	if state.Mode == ModeSearch {
		t.Fatal("/ in the help overlay switched the keymap to search mode")
	}

	// a locked mode too: an overlay it opened owns the keyboard
	press(o, esc, esc) // leave the help's search, close the help
	state.Mode, state.Locked = ModeTabs, true
	o.ShowHelp = true // as if an action of the locked mode had opened it
	press(o, esc)
	if o.ShowHelp || state.Mode != ModeTyping || state.Locked {
		t.Fatalf("esc over an overlay in a locked mode: help open=%v, mode=%v, locked=%v",
			o.ShowHelp, state.Mode, state.Locked)
	}
}

func TestWithoutAnOverlayTheModesStillWork(t *testing.T) {
	state, o := installForTest(t)

	press(o, ctrlG, tea.KeyPressMsg{Code: 'p', Text: "p"})
	if state.Mode != ModePanes {
		t.Fatalf("ctrl+g p: mode %v, want Panes", state.Mode)
	}
	press(o, esc)
	if state.Mode != ModeTyping {
		t.Fatalf("esc from panes mode: mode %v, want Typing", state.Mode)
	}
}
