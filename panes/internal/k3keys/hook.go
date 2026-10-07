package k3keys

import (
	"log"

	tea "charm.land/bubbletea/v2"

	"github.com/Gaurav-Gosain/tuios/internal/app"
	"github.com/Gaurav-Gosain/tuios/internal/input"
)

// Install sets up the k3keys PreHandler hook into tuios input package.
// It sets the PreHandler on input. The LegendOverride on app must be set
// separately by the caller (in cmd/k3/main.go) since k3keys doesn't import app.
func Install(state *KeyState, bindings map[Mode]map[string]string) {
	// PreHandler intercepts key events before tuios processes them
	input.PreHandler = func(msg tea.Msg, o *app.OS) (bool, tea.Model, tea.Cmd) {
		keyMsg, ok := msg.(tea.KeyPressMsg)
		if !ok {
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
}
