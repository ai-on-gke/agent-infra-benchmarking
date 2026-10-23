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

package main

import (
	"slices"
	"testing"
)

// runworkload is built by main rather than registered with userclass, but
// --user-class still has to offer it beside the upstream classes.
func TestUserClassesListRunworkload(t *testing.T) {
	names := userClasses()
	for _, want := range []string{"runworkload", "glutton"} {
		if !slices.Contains(names, want) {
			t.Errorf("user classes: got %v, want %q among them", names, want)
		}
	}
	if !slices.IsSorted(names) {
		t.Errorf("user classes: got %v, want them sorted", names)
	}
}
