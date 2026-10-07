package k3keys

import (
	"os"
	"path/filepath"
	"strings"

	"github.com/pelletier/go-toml/v2"
)

// Config represents the k3keys configuration loaded from ~/.config/k3/keys.toml
type Config struct {
	Panes map[string]string `toml:"panes"`
}

// LoadConfig loads the k3keys config from ~/.config/k3/keys.toml
// Returns empty config if file doesn't exist or has errors.
func LoadConfig() Config {
	configPath := getConfigPath()
	if configPath == "" {
		return Config{}
	}

	data, err := os.ReadFile(configPath)
	if err != nil {
		return Config{}
	}

	var cfg Config
	if err := toml.Unmarshal(data, &cfg); err != nil {
		return Config{}
	}

	return cfg
}

// getConfigPath returns the path to the k3 keys config file
func getConfigPath() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return filepath.Join(home, ".config", "k3", "keys.toml")
}

// ParseUserBindings converts the flat config.Panes map into the mode-key-action format.
// Expected format: [panes] key = "action" (for typing mode)
// For other modes, use prefix: mode:key = "action" e.g. "panes:n" = "new_window"
func ParseUserBindings(cfg Config) map[Mode]map[string]string {
	result := make(map[Mode]map[string]string)

	for key, action := range cfg.Panes {
		mode := ModeTyping
		keyName := key

		// Check for mode prefix (e.g., "panes:n", "tabs:1", "resize:left")
		if idx := strings.Index(key, ":"); idx > 0 {
			modeStr := key[:idx]
			keyName = key[idx+1:]
			switch strings.ToLower(modeStr) {
			case "typing":
				mode = ModeTyping
			case "chooser":
				mode = ModeChooser
			case "panes":
				mode = ModePanes
			case "tabs":
				mode = ModeTabs
			case "sessions":
				mode = ModeSessions
			case "resize":
				mode = ModeResize
			case "search":
				mode = ModeSearch
			case "agents":
				mode = ModeAgents
			}
		}

		if result[mode] == nil {
			result[mode] = make(map[string]string)
		}
		result[mode][keyName] = action
	}

	return result
}
