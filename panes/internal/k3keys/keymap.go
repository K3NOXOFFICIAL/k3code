package k3keys

import "strings"

// Mode represents the current keymap mode.
type Mode int

const (
	ModeTyping Mode = iota
	ModeChooser
	ModePanes
	ModeTabs
	ModeSessions
	ModeResize
	ModeSearch
	ModeAgents
)

// String returns the mode name for debugging.
func (m Mode) String() string {
	switch m {
	case ModeTyping:
		return "Typing"
	case ModeChooser:
		return "Chooser"
	case ModePanes:
		return "Panes"
	case ModeTabs:
		return "Tabs"
	case ModeSessions:
		return "Sessions"
	case ModeResize:
		return "Resize"
	case ModeSearch:
		return "Search"
	case ModeAgents:
		return "Agents"
	default:
		return "Unknown"
	}
}

// defaultBindings is the core keymap table: mode -> key -> action.
// Internal pseudo-actions (k3:lock, k3:typing) are handled specially.
var defaultBindings = map[Mode]map[string]string{
	ModeTyping: {
		"ctrl+g":    "k3:chooser",
		"alt+left":  "terminal_focus_left",
		"alt+right": "terminal_focus_right",
		"alt+up":    "terminal_focus_up",
		"alt+down":  "terminal_focus_down",
		"alt+n":     "new_window",
		"alt+1":     "switch_workspace_1",
		"alt+2":     "switch_workspace_2",
		"alt+3":     "switch_workspace_3",
		"alt+4":     "switch_workspace_4",
		"alt+5":     "switch_workspace_5",
		"alt+6":     "switch_workspace_6",
		"alt+7":     "switch_workspace_7",
		"alt+8":     "switch_workspace_8",
		"alt+9":     "switch_workspace_9",
		"alt+z":     "toggle_zoom",
		"ctrl+p":    "command_palette",
	},
	ModeChooser: {
		"p":   "k3:mode:panes",
		"t":   "k3:mode:tabs",
		"s":   "k3:mode:sessions",
		"r":   "k3:mode:resize",
		"/":   "k3:mode:search",
		"a":   "k3:mode:agents",
		"?":   "toggle_help",
		"esc": "k3:typing",
	},
	ModePanes: {
		"n":     "new_window",
		"x":     "close_window",
		"v":     "split_vertical",
		"h":     "split_horizontal",
		"left":  "terminal_focus_left",
		"right": "terminal_focus_right",
		"up":    "terminal_focus_up",
		"down":  "terminal_focus_down",
		"z":     "toggle_zoom",
		"f":     "toggle_tiling",
		"=":     "equalize_splits",
		"p":     "k3:lock",
		"esc":   "k3:typing",
	},
	ModeTabs: {
		"n":   "next_workspace",
		"p":   "prev_workspace",
		"1":   "switch_workspace_1",
		"2":   "switch_workspace_2",
		"3":   "switch_workspace_3",
		"4":   "switch_workspace_4",
		"5":   "switch_workspace_5",
		"6":   "switch_workspace_6",
		"7":   "switch_workspace_7",
		"8":   "switch_workspace_8",
		"9":   "switch_workspace_9",
		"r":   "rename_workspace",
		"t":   "k3:lock",
		"esc": "k3:typing",
	},
	ModeSessions: {
		"n":   "new_session",
		"j":   "next_session",
		"k":   "prev_session",
		"w":   "prefix_session_switcher",
		"r":   "rename_session",
		"d":   "prefix_detach",
		"s":   "k3:lock",
		"esc": "k3:typing",
	},
	ModeResize: {
		"left":  "resize_height_shrink",
		"right": "resize_height_grow",
		"up":    "resize_master_grow",
		"down":  "resize_master_shrink",
		"r":     "k3:lock",
		"esc":   "k3:typing",
	},
	ModeSearch: {
		"/":   "prefix_scrollback",
		"i":   "prefix_inbox",
		"s":   "k3:lock",
		"esc": "k3:typing",
	},
	ModeAgents: {
		"i":   "prefix_inbox",
		"n":   "prefix_next_attention",
		"s":   "prefix_agents_settings",
		"a":   "k3:lock",
		"esc": "k3:typing",
	},
}

// globalKeys are the typing-mode keys that also work in every other mode.
var globalKeys = map[string]bool{
	"alt+left": true, "alt+right": true, "alt+up": true, "alt+down": true,
	"alt+n": true, "alt+z": true, "ctrl+p": true,
	"alt+1": true, "alt+2": true, "alt+3": true, "alt+4": true, "alt+5": true,
	"alt+6": true, "alt+7": true, "alt+8": true, "alt+9": true,
}

// KeyState holds the current keymap state.
type KeyState struct {
	Mode   Mode
	Locked bool
}

// NewKeyState creates a new KeyState starting in Typing mode.
func NewKeyState() *KeyState {
	return &KeyState{
		Mode:   ModeTyping,
		Locked: false,
	}
}

// Handle processes a key and returns the list of tuios actions to run,
// plus whether the key was consumed (true) or should pass through (false).
func (s *KeyState) Handle(key string, bindings map[Mode]map[string]string) ([]string, bool) {
	// Normalize key
	key = strings.ToLower(strings.TrimSpace(key))

	// Get bindings for current mode
	modeBindings := bindings[s.Mode]
	if modeBindings == nil {
		modeBindings = map[string]string{}
	}

	// Global keys work in every mode. A binding in the current mode wins;
	// otherwise the typing-mode binding runs (so user overrides apply) and the
	// mode stays as it is.
	if _, ok := modeBindings[key]; !ok && s.Mode != ModeTyping && globalKeys[key] {
		if action, ok := bindings[ModeTyping][key]; ok && action != "" {
			if strings.HasPrefix(action, "k3:") || action == "esc" {
				return s.handleAction(action)
			}
			return []string{action}, true
		}
	}

	// Check for action in current mode
	if action, ok := modeBindings[key]; ok {
		return s.handleAction(action)
	}

	// In Typing mode, everything else passes through
	if s.Mode == ModeTyping {
		return nil, false
	}

	// In other modes, unrecognized keys do nothing but are consumed (don't pass through)
	return nil, true
}

// handleAction processes an action (real or pseudo) and updates state accordingly.
// Returns the list of tuios actions to execute and whether the key was consumed.
func (s *KeyState) handleAction(action string) ([]string, bool) {
	switch action {
	case "k3:chooser":
		s.Mode = ModeChooser
		s.Locked = false
		return nil, true

	case "k3:mode:panes":
		s.Mode = ModePanes
		s.Locked = false
		return nil, true

	case "k3:mode:tabs":
		s.Mode = ModeTabs
		s.Locked = false
		return nil, true

	case "k3:mode:sessions":
		s.Mode = ModeSessions
		s.Locked = false
		return nil, true

	case "k3:mode:resize":
		s.Mode = ModeResize
		s.Locked = true // Resize is lock-by-default
		return nil, true

	case "k3:mode:search":
		s.Mode = ModeSearch
		s.Locked = false
		return nil, true

	case "k3:mode:agents":
		s.Mode = ModeAgents
		s.Locked = false
		return nil, true

	case "k3:lock":
		s.Locked = true
		return nil, true

	case "k3:typing":
		s.Mode = ModeTyping
		s.Locked = false
		// After returning to typing mode, we need to re-enter terminal mode
		// so keys go to the pane again. This is a special marker action.
		return []string{"enter_terminal_mode"}, true

	case "esc":
		// Escape always returns to typing mode
		s.Mode = ModeTyping
		s.Locked = false
		return []string{"enter_terminal_mode"}, true

	default:
		// Real tuios action
		actions := []string{action}

		// After a one-shot action in a non-locked mode, return to typing
		if !s.Locked && s.Mode != ModeTyping && s.Mode != ModeChooser {
			s.Mode = ModeTyping
			s.Locked = false
			actions = append(actions, "enter_terminal_mode")
		}

		return actions, true
	}
}

// MergeBindings merges user overrides into the default bindings.
// User bindings take precedence.
func MergeBindings(userBindings map[Mode]map[string]string) map[Mode]map[string]string {
	result := make(map[Mode]map[string]string, len(defaultBindings))
	for mode, binds := range defaultBindings {
		result[mode] = make(map[string]string, len(binds))
		for k, v := range binds {
			result[mode][k] = v
		}
	}
	for mode, binds := range userBindings {
		if result[mode] == nil {
			result[mode] = make(map[string]string)
		}
		for k, v := range binds {
			result[mode][k] = v
		}
	}
	return result
}
