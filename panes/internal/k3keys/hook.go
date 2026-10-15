package k3keys

import (
	"log"

	tea "charm.land/bubbletea/v2"

	"github.com/Gaurav-Gosain/tuios/internal/app"
	"github.com/Gaurav-Gosain/tuios/internal/input"
	"github.com/Gaurav-Gosain/tuios/internal/overlay"
)

// Install sets up the k3keys hooks into tuios: both of them, per the spec.
// input.PreHandler intercepts key events before tuios processes them;
// app.LegendOverride replaces the dock's mode legend with the k3 keys of the
// current mode. k3keys touches app and input, but neither may import k3keys
// (the reason this lives on hook variables rather than calls).
func Install(state *KeyState, bindings map[Mode]map[string]string) {
	// PreHandler intercepts key events before tuios processes them
	input.PreHandler = func(msg tea.Msg, o *app.OS) (bool, tea.Model, tea.Cmd) {
		keyMsg, ok := msg.(tea.KeyPressMsg)
		if !ok {
			return false, nil, nil
		}

		// An open overlay owns the keyboard, as it does in tuios itself. A
		// mode's action can open one (ctrl+g ? opens the help, a locked
		// mode's rename opens an editor) and leave the keymap in that mode,
		// which took every key first: esc only switched back to typing and
		// left the overlay up, and the overlay's own keys ran mode actions
		// (#170). The mode's work is done once the overlay is up, so the
		// keymap goes back to typing and the key goes to the overlay.
		if state.Mode != ModeTyping && overlayOwnsKeys(o) {
			state.Mode, state.Locked = ModeTyping, false
			return false, nil, nil
		}

		// Convert key to string (e.g., "ctrl+g", "alt+left", "p")
		key := keyMsg.String()

		// Process through our keymap
		actions, consumed := state.Handle(key, bindings)
		if !consumed {
			return false, nil, nil
		}

		// Run all returned actions
		var cmds []tea.Cmd
		for _, action := range actions {
			handled, cmd := input.RunActionByName(action, o)
			if !handled {
				log.Printf("k3keys: action %q not handled", action)
			}
			if cmd != nil {
				cmds = append(cmds, cmd)
			}
		}

		// Batch all commands
		if len(cmds) > 0 {
			return true, o, tea.Batch(cmds...)
		}
		return true, o, nil
	}

	// LegendOverride replaces the dock's mode legend with the k3 keys of the
	// current mode. The conversion to overlay.Hint happens here, at the
	// package boundary; the pure-Go Hint type stays overlay-free.
	app.LegendOverride = func(o *app.OS) []overlay.Hint {
		k3hints := Hints(state.Mode, state.Locked)
		hints := make([]overlay.Hint, len(k3hints))
		for i, h := range k3hints {
			hints[i] = overlay.Hint{
				Key:      h.Key,
				Label:    h.Label,
				Priority: overlay.HintPriority(h.Priority),
			}
		}
		return hints
	}
}

// overlayOwnsKeys reports whether tuios routes keys to an overlay, an editor
// or a full-screen gesture rather than to the panes and the window bindings
// (see routeKey and HandleKeyPress in internal/input/handler.go).
func overlayOwnsKeys(o *app.OS) bool {
	return o.AnyOverlayOpen() || o.Renaming() || o.ContextMenuActive() || o.ReviewOpen() ||
		o.HintsOpen() || o.PaneLabelsOpen() || o.CaptureActive() || o.ScreenshotPreviewOpen() ||
		o.ShowScrollbackBrowser || o.ShowTapeReview || o.ShowTapeManager
}
