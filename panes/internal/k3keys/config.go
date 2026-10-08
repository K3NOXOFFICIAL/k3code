package k3keys

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/pelletier/go-toml/v2"
)

// Config represents the k3keys configuration loaded from ~/.config/k3/keys.toml.
// Panes holds every binding, from flat top-level `key = "action"` pairs (the
// documented form) and from an optional [panes] table.
type Config struct {
	Panes map[string]string `toml:"panes"`
}

// warnOut is where config problems are reported; LoadConfig runs before the TUI
// takes over the screen, so stderr is visible.
var warnOut io.Writer = os.Stderr

// LoadConfig loads the k3keys config from ~/.config/k3/keys.toml.
// Returns an empty config if the file doesn't exist; a file that can't be read
// or parsed is reported on stderr and ignored.
func LoadConfig() Config {
	configPath := getConfigPath()
	if configPath == "" {
		return Config{}
	}

	data, err := os.ReadFile(configPath)
	if err != nil {
		if !errors.Is(err, fs.ErrNotExist) {
			fmt.Fprintf(warnOut, "k3: %s: %v (key overrides ignored)\n", configPath, err)
		}
		return Config{}
	}

	cfg, err := parseConfig(data)
	if err != nil {
		fmt.Fprintf(warnOut, "k3: %s: %v (key overrides ignored)\n", configPath, err)
		return Config{}
	}
	return cfg
}

// parseConfig accepts top-level `key = "action"` pairs and a [panes] table.
func parseConfig(data []byte) (Config, error) {
	var raw map[string]any
	if err := toml.Unmarshal(data, &raw); err != nil {
		return Config{}, err
	}
	cfg := Config{Panes: map[string]string{}}
	keys := make([]string, 0, len(raw))
	for k := range raw {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		switch v := raw[k].(type) {
		case string:
			cfg.Panes[k] = v
		case map[string]any:
			if k != "panes" {
				fmt.Fprintf(warnOut, "k3: keys.toml: unknown table [%s] ignored\n", k)
				continue
			}
			for pk, pv := range v {
				s, ok := pv.(string)
				if !ok {
					fmt.Fprintf(warnOut, "k3: keys.toml: [panes] %q is not a string, ignored\n", pk)
					continue
				}
				cfg.Panes[pk] = s
			}
		default:
			fmt.Fprintf(warnOut, "k3: keys.toml: %q is not a string, ignored\n", k)
		}
	}
	return cfg, nil
}

// getConfigPath returns the path to the k3 keys config file
func getConfigPath() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return filepath.Join(home, ".config", "k3", "keys.toml")
}

var modeNames = map[string]Mode{
	"typing":   ModeTyping,
	"chooser":  ModeChooser,
	"panes":    ModePanes,
	"tabs":     ModeTabs,
	"sessions": ModeSessions,
	"resize":   ModeResize,
	"search":   ModeSearch,
	"agents":   ModeAgents,
}

// ParseUserBindings converts the flat config.Panes map into the mode-key-action format.
// Expected format: key = "action" (for typing mode)
// For other modes, use prefix: mode:key = "action" e.g. "panes:n" = "new_window".
// An unknown mode prefix is reported and skipped, so a typo never rebinds a
// bare key in typing mode. Keys are lowercased to match Handle.
func ParseUserBindings(cfg Config) map[Mode]map[string]string {
	result := make(map[Mode]map[string]string)

	for key, action := range cfg.Panes {
		mode := ModeTyping
		keyName := key

		// Check for mode prefix (e.g., "panes:n", "tabs:1", "resize:left")
		if idx := strings.Index(key, ":"); idx > 0 {
			m, ok := modeNames[strings.ToLower(key[:idx])]
			if !ok {
				fmt.Fprintf(warnOut, "k3: keys.toml: unknown mode in %q, ignored\n", key)
				continue
			}
			mode = m
			keyName = key[idx+1:]
		}
		keyName = strings.ToLower(strings.TrimSpace(keyName))
		if keyName == "" {
			continue
		}

		if result[mode] == nil {
			result[mode] = make(map[string]string)
		}
		result[mode][keyName] = action
	}

	return result
}
