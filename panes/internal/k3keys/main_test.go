package k3keys

import (
	"os"
	"testing"

	"github.com/Gaurav-Gosain/tuios/internal/testutil"
)

// TestMain isolates the whole test binary from the developer's own XDG
// directories. See testutil.RunIsolated for why this cannot be a per-test
// helper. It matters here: LoadConfig reads ~/.config/k3/keys.toml, so an
// unisolated run would load whatever overrides the developer has bound.
func TestMain(m *testing.M) { os.Exit(testutil.RunIsolated(m)) }
