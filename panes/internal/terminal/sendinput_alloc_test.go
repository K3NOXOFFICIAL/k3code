package terminal

import "testing"

// TestSendInputAllocatesNothingWithoutDebug pins the cost of a keystroke on
// its way to the PTY. The debug log line SendInput can write was having its
// arguments built on every call, a timestamp, a string copy of the input and
// a hex rendering, before the switch that decides whether to write it was
// read. With the switch off, which is every run but a debugging one, a
// keystroke must not allocate here at all.
func TestSendInputAllocatesNothingWithoutDebug(t *testing.T) {
	t.Setenv("TUIOS_DEBUG_INTERNAL", "")
	if debugInternal() {
		t.Skip("TUIOS_DEBUG_INTERNAL was read as set before this test ran")
	}
	key := []byte("x")

	local := &Window{ID: "sendinput-alloc-local"}
	local.Pty = &nopPty{}
	if got := testing.AllocsPerRun(1000, func() { _ = local.SendInput(key) }); got != 0 {
		t.Fatalf("a keystroke to a local PTY allocates %.1f times, want 0", got)
	}

	daemon := &Window{ID: "sendinput-alloc-daemon", DaemonMode: true}
	daemon.DaemonWriteFunc = func([]byte) error { return nil }
	if got := testing.AllocsPerRun(1000, func() { _ = daemon.SendInput(key) }); got != 0 {
		t.Fatalf("a keystroke to a daemon pane allocates %.1f times, want 0", got)
	}
}
