package k3keys

// Hint represents a key hint for display.
// This is a pure Go type (no tuios dependencies) for unit testability.
type Hint struct {
	Key      string
	Label    string
	Priority int8 // -1 = optional, 0 = normal, 1 = essential
}

const (
	HintOptional  = -1
	HintNormal    = 0
	HintEssential = 1
)

// Hints returns the hint slice to display for the given mode and lock state.
// This returns k3keys.Hint (pure Go type) for unit testability.
// The caller (cmd/k3/main.go) converts to overlay.Hint for the LegendOverride hook.
func Hints(mode Mode, locked bool) []Hint {
	var hints []Hint

	// Helper to add a hint with an optional lock marker (ASCII only: the
	// upstream no-emoji lint rejects any pictograph in source strings).
	add := func(key, label string, priority int8) {
		if locked {
			label += " [locked]"
		}
		hints = append(hints, Hint{Key: key, Label: label, Priority: priority})
	}

	switch mode {
	case ModeTyping:
		add("ctrl+g", "modes", HintNormal)
		add("alt+←/→", "focus", HintNormal)
		add("alt+n", "new", HintNormal)
		add("ctrl+p", "palette", HintNormal)

	case ModeChooser:
		add("p", "panes", HintNormal)
		add("t", "tabs", HintNormal)
		add("s", "sessions", HintNormal)
		add("r", "resize", HintNormal)
		add("/", "search", HintNormal)
		add("a", "agents", HintNormal)
		add("?", "help", HintNormal)
		add("esc", "back", HintEssential)

	case ModePanes:
		add("n", "new", HintNormal)
		add("x", "close", HintNormal)
		add("v", "v-split", HintNormal)
		add("h", "h-split", HintNormal)
		add("←/→/↑/↓", "focus", HintNormal)
		add("z", "zoom", HintNormal)
		add("f", "tiling", HintNormal)
		add("=", "equalize", HintNormal)
		add("p", "lock", HintNormal)
		add("esc", "back", HintEssential)

	case ModeTabs:
		add("n", "next", HintNormal)
		add("p", "prev", HintNormal)
		add("1-9", "switch", HintNormal)
		add("r", "rename", HintNormal)
		add("t", "lock", HintNormal)
		add("esc", "back", HintEssential)

	case ModeSessions:
		add("n", "new", HintNormal)
		add("j/k", "next/prev", HintNormal)
		add("w", "switcher", HintNormal)
		add("r", "rename", HintNormal)
		add("d", "detach", HintNormal)
		add("s", "lock", HintNormal)
		add("esc", "back", HintEssential)

	case ModeResize:
		add("←/→", "height", HintNormal)
		add("↑/↓", "master", HintNormal)
		add("r", "lock", HintNormal)
		add("esc", "exit", HintEssential)

	case ModeSearch:
		add("/", "scrollback", HintNormal)
		add("i", "inbox", HintNormal)
		add("s", "lock", HintNormal)
		add("esc", "exit", HintEssential)

	case ModeAgents:
		add("a", "lock", HintNormal)
		add("esc", "exit", HintEssential)
	}

	return hints
}

// ModeLabel returns a human-readable label for the mode (used in the hint prefix).
func ModeLabel(mode Mode) string {
	switch mode {
	case ModeTyping:
		return "TYPING"
	case ModeChooser:
		return "MODES"
	case ModePanes:
		return "PANES"
	case ModeTabs:
		return "TABS"
	case ModeSessions:
		return "SESSIONS"
	case ModeResize:
		return "RESIZE"
	case ModeSearch:
		return "SEARCH"
	case ModeAgents:
		return "AGENTS"
	default:
		return "UNKNOWN"
	}
}
