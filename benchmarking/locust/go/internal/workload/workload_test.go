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
		if _, ok := w.(gluttonWorkload); !ok {
			t.Errorf("workload: got %T, want gluttonWorkload", w)
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
