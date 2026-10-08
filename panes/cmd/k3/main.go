// Package main implements the k3 keymap variant of TUIOS.
// This is a minimal entry point that installs the k3keys keymap and runs the TUI.
package main

import (
	"fmt"
	"log"
	"os"
	"os/signal"
	"runtime/pprof"
	"syscall"

	tea "charm.land/bubbletea/v2"
	"github.com/Gaurav-Gosain/tuios/internal/app"
	"github.com/Gaurav-Gosain/tuios/internal/config"
	"github.com/Gaurav-Gosain/tuios/internal/debuglog"
	"github.com/Gaurav-Gosain/tuios/internal/input"
	"github.com/Gaurav-Gosain/tuios/internal/k3keys"
	"github.com/Gaurav-Gosain/tuios/internal/terminal"
)

var (
	version = "dev"
	commit  = "none"
	date    = "unknown"
	builtBy = "unknown"

	debugMode  bool
	cpuProfile string
	pprofAddr  string
)

const helpText = `k3 - TUIOS with the k3 modal keymap (full reference: panes/docs/k3-keymap.md in the k3code repo)

Usage: k3 [-h|--help]

Keys: ctrl+g opens the Chooser: p panes, t tabs, s sessions, r resize,
/ search, a agents, ? help. esc returns to typing.

Agents (ctrl+g a):
  i  Inbox: approvals and questions from agents in other panes
  n  jump to the next pane that is waiting for you
  s  agent settings
  a  lock the mode
A pane running an agent (k3code, claude, codex, ...) carries a state badge:
working, needs input, idle, done or errored. k3code reports its own state
and, with "k3code" in [agents.approvals] enabled, sends its approval prompts
to the Inbox when its pane is not in front of you. In a tuios daemon session
k3code can open /bg --pane and /fork --pane sessions in new panes.
`

func main() {
	for _, a := range os.Args[1:] {
		if a == "-h" || a == "--help" {
			fmt.Print(helpText)
			return
		}
	}

	// The build identity, handed to internal/app before anything can crash.
	app.SetBuildStamp(version, commit)

	// Set up the k3keys keymap
	k3state := k3keys.NewKeyState()
	userBindings := k3keys.LoadConfig()
	parsedBindings := k3keys.ParseUserBindings(userBindings)
	bindings := k3keys.MergeBindings(parsedBindings)

	// Install the k3 keys and both hooks (key handling and legend).
	k3keys.Install(k3state, bindings)

	if cpuProfile != "" {
		f, err := os.Create(cpuProfile)
		if err != nil {
			log.Fatalf("could not create CPU profile: %v", err)
		}
		defer f.Close()
		if err := pprof.StartCPUProfile(f); err != nil {
			log.Fatalf("could not start CPU profile: %v", err)
		}
		defer pprof.StopCPUProfile()
	}

	startPprofServer()

	if err := runLocal(); err != nil {
		log.Fatalf("program error: %v", err)
	}
}

func loadAndApplyConfig() *config.UserConfig {
	userConfig, err := config.LoadUserConfig()
	if err != nil {
		log.Printf("Warning: Failed to load config, using defaults: %v", err)
		userConfig = config.DefaultConfig()
	}

	env, why := config.DetectGlyphEnv(os.Getenv)
	config.Global.GlyphEnv = env
	if why != "" {
		log.Printf("glyphs: %s, so the chrome is drawn with %s glyphs unless a glyph set or --ascii-only is chosen", why, env)
	}

	config.ApplyAppearanceConfig(userConfig, &config.Global)
	// No CLI flag overrides for k3 binary

	return userConfig
}

func runLocal() error {
	if err := checkTerminal(); err != nil {
		return err
	}

	if debugMode {
		_ = os.Setenv("TUIOS_DEBUG_INTERNAL", "1")
		fmt.Println("Debug mode enabled")
	}

	if os.Getenv("TUIOS_DEBUG_INTERNAL") == "1" {
		if lf, lerr := debuglog.Open(debuglog.Path); lerr == nil {
			log.SetOutput(lf)
		}
	}

	userConfig := loadAndApplyConfig()

	app.SetInputHandler(input.HandleInput)

	keybindRegistry := config.NewKeybindRegistry(userConfig)

	isDaemonSession := os.Getenv("TUIOS_SESSION") != ""

	prw := app.NewPostRenderWriter(os.Stdout)

	initialOS := app.NewOS(app.OSOptions{
		Client:          app.ClientLocal,
		KeybindRegistry: keybindRegistry,
		UserConfig:      userConfig,
		ShowKeys:        false,
		IsDaemonSession: isDaemonSession,
		GraphicsOutput:  prw,
	})
	initialOS.PostRenderWriter = prw
	initialOS.ConnectFrameWriter(prw)

	p := tea.NewProgram(initialOS, append(app.ProgramOptions(), tea.WithOutput(prw))...)
	initialOS.BindProgram(p)

	finish := armSignalQuit(p)

	restoreTabs := withoutHardTabs()
	finalModel, err := p.Run()
	restoreTabs()

	if finalOS, ok := finalModel.(*app.OS); ok {
		finalOS.DumpTickStats()
		finalOS.Cleanup()
	}

	terminal.ResetTerminal()
	finish()

	if err != nil {
		return fmt.Errorf("program error: %w", err)
	}

	return nil
}

func checkTerminal() error {
	if _, err := os.Stdout.Stat(); err != nil {
		return err
	}
	return nil
}

func armSignalQuit(p *tea.Program) func() {
	c := make(chan os.Signal, 1)
	signal.Notify(c, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-c
		p.Quit()
	}()
	return func() {
		signal.Stop(c)
		close(c)
	}
}

func withoutHardTabs() func() {
	// Simplified - no hard tab handling for k3
	return func() {}
}

func startPprofServer() {
	// No pprof server for k3 binary by default
}
