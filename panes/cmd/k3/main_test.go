//go:build unix

package main

import (
	"bytes"
	"context"
	"errors"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/Gaurav-Gosain/tuios/internal/testutil"
)

// runMainEnv makes the test binary run main() with the arguments in it instead
// of the tests, so the whole command line path is checked, not one helper.
const runMainEnv = "K3_TEST_RUN_MAIN_ARGS"

// TestMain also isolates the whole test binary from the developer's own XDG
// directories. See testutil.RunIsolated for why this cannot be a per-test
// helper. The re-executed main() below inherits the isolated environment.
func TestMain(m *testing.M) {
	if args, ok := os.LookupEnv(runMainEnv); ok {
		os.Args = []string{"k3"}
		if args != "" {
			os.Args = append(os.Args, strings.Split(args, "\x1f")...)
		}
		main()
		os.Exit(0)
	}
	os.Exit(testutil.RunIsolated(m))
}

// runK3 runs k3 with args and no terminal: no stdin and no controlling tty,
// like `k3 --version </dev/null` from a script. A k3 that starts the TUI here
// fails in bubbletea at once; the timeout only stops a hang from outliving the
// test.
func runK3(t *testing.T, args ...string) (stdout, stderr string, code int) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^$")
	cmd.Env = append(os.Environ(), runMainEnv+"="+strings.Join(args, "\x1f"))
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	var out, errb bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &errb
	err := cmd.Run()
	if ctx.Err() != nil {
		t.Fatalf("k3 %q did not exit in 30s (it started the TUI?)\nstdout: %q\nstderr: %q", args, out.String(), errb.String())
	}
	var ee *exec.ExitError
	switch {
	case err == nil:
	case errors.As(err, &ee):
		code = ee.ExitCode()
	default:
		t.Fatalf("running k3 %q: %v", args, err)
	}
	return out.String(), errb.String(), code
}

func TestVersionPrintsWithoutStartingTheTUI(t *testing.T) {
	stdout, stderr, code := runK3(t, "--version")
	if code != 0 {
		t.Fatalf("exit code = %d, want 0\nstdout: %q\nstderr: %q", code, stdout, stderr)
	}
	if !strings.HasPrefix(stdout, "k3 ") || !strings.Contains(stdout, version) {
		t.Errorf("stdout = %q, want a line that starts with \"k3 \" and names the version %q", stdout, version)
	}
	if strings.ContainsRune(stdout+stderr, '\x1b') {
		t.Errorf("output holds terminal escape sequences: stdout %q stderr %q", stdout, stderr)
	}
	if stderr != "" {
		t.Errorf("stderr = %q, want nothing", stderr)
	}
}

func TestUnknownArgumentFailsWithoutStartingTheTUI(t *testing.T) {
	for _, args := range [][]string{{"--bogus"}, {"-x"}, {"stray"}, {"--bogus", "--help"}} {
		stdout, stderr, code := runK3(t, args...)
		if code != 2 {
			t.Errorf("k3 %q: exit code = %d, want 2\nstdout: %q\nstderr: %q", args, code, stdout, stderr)
		}
		if !strings.Contains(stderr, "unknown argument") || !strings.Contains(stderr, args[0]) {
			t.Errorf("k3 %q: stderr = %q, want it to name the unknown argument", args, stderr)
		}
		if stdout != "" || strings.ContainsRune(stderr, '\x1b') {
			t.Errorf("k3 %q: stdout %q stderr %q, want no stdout and no escape sequences", args, stdout, stderr)
		}
	}
}

func TestHelpStillPrints(t *testing.T) {
	for _, flag := range []string{"-h", "--help"} {
		stdout, stderr, code := runK3(t, flag)
		if code != 0 || stderr != "" || !strings.HasPrefix(stdout, "k3 - TUIOS") {
			t.Errorf("k3 %s: code %d, stdout %q, stderr %q, want the help text and exit 0", flag, code, stdout, stderr)
		}
	}
}
