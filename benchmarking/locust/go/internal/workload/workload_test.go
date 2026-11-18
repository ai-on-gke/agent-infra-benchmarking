// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package workload

import (
	"context"
	"flag"
	"strings"
	"testing"
)

func TestResolve(t *testing.T) {
	t.Run("unset selects the default", func(t *testing.T) {
		w, err := Resolve("")
		if err != nil {
			t.Fatalf("Resolve(\"\") failed: %v", err)
		}
		if w.Name() != Default {
			t.Errorf("workload: got %q, want %q", w.Name(), Default)
		}
	})

	t.Run("names the registered workload", func(t *testing.T) {
		w, err := Resolve(GluttonName)
		if err != nil {
			t.Fatalf("Resolve(%q) failed: %v", GluttonName, err)
		}
		if _, ok := w.(*gluttonWorkload); !ok {
			t.Errorf("workload: got %T, want *gluttonWorkload", w)
		}
	})

	// A typo that fell back to the default would produce rows that look valid
	// while measuring a workload nobody asked for, so it has to be an error —
	// and the message has to say what could have been asked for instead.
	t.Run("unknown name is an error listing the known ones", func(t *testing.T) {
		_, err := Resolve("not-a-workload")
		if err == nil {
			t.Fatal("Resolve succeeded for an unregistered name, want error")
		}
		if !strings.Contains(err.Error(), GluttonName) {
			t.Errorf("error does not list the known workloads: %v", err)
		}
	})
}

func TestConfigure(t *testing.T) {
	// parse registers every workload's flags on a new set and parses args.
	parse := func(t *testing.T, args ...string) *flag.FlagSet {
		t.Helper()
		fs := flag.NewFlagSet("worker", flag.ContinueOnError)
		RegisterFlags(fs)
		if err := fs.Parse(args); err != nil {
			t.Fatalf("Parse(%q) failed: %v", args, err)
		}
		return fs
	}
	glutton, _ := Resolve(GluttonName)
	claudeCode, _ := Resolve(claudeCodeWorkloadName)
	t.Cleanup(func() { *glutton.(*gluttonWorkload) = gluttonWorkload{} })

	t.Run("applies the workload's own flags", func(t *testing.T) {
		fs := parse(t, "--glutton-write-ram=1Mi", "--glutton-write-disk=4096")
		if err := Configure(context.Background(), glutton, fs); err != nil {
			t.Fatalf("Configure failed: %v", err)
		}
		var metrics []string
		for _, l := range glutton.(Loader).Loads() {
			metrics = append(metrics, l.Metric)
		}
		if got, want := strings.Join(metrics, ","), "WriteRAM,WriteDisk"; got != want {
			t.Errorf("loads: got %s, want %s", got, want)
		}
	})

	t.Run("refuses another workload's flag", func(t *testing.T) {
		fs := parse(t, "--glutton-write-ram=1Mi")
		err := Configure(context.Background(), claudeCode, fs)
		if err == nil || !strings.Contains(err.Error(), "--glutton-write-ram is glutton's, not claude-code's") {
			t.Errorf("err: got %v, want --glutton-write-ram refused", err)
		}
	})

	t.Run("refuses a negative disk write", func(t *testing.T) {
		fs := parse(t, "--glutton-write-disk=-1")
		if err := Configure(context.Background(), glutton, fs); err == nil {
			t.Error("Configure succeeded, want --glutton-write-disk refused")
		}
	})
}
