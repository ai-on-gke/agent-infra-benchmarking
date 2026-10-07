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

// testcase-helper prints one case of a benchmark tests.yaml as JSON.
//
// It ships in the locust image beside the tests.yaml it reads, so that
// run_locust_job.sh can resolve a case through the image rather than needing a
// YAML parser on the host: the machine starting a run has kubectl and docker,
// and not necessarily PyYAML or yq. runner.py reads the same file in the pod.
//
// An unknown case is a non-zero exit naming the cases the file does hold,
// which is what lets a run fail before it rebuilds the WorkerPool and submits
// the Job, rather than in the pod once both have happened.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"sort"
	"strings"

	"sigs.k8s.io/yaml"
)

// Where the Dockerfile puts tests.yaml. The flag exists so the binary is also
// usable against a working tree.
const defaultPath = "/app/tests.yaml"

type doc struct {
	Cases map[string]map[string]any `json:"cases"`
}

func main() {
	path := flag.String("f", defaultPath, "Path to the tests file.")
	name := flag.String("case", "", "Case to print as JSON. Required unless --list.")
	list := flag.Bool("list", false, "Print every case name, one per line, and exit.")
	flag.Parse()

	raw, err := os.ReadFile(*path)
	if err != nil {
		fatal("cannot read %s: %v", *path, err)
	}
	var d doc
	if err := yaml.Unmarshal(raw, &d); err != nil {
		fatal("%s is not valid YAML: %v", *path, err)
	}
	if len(d.Cases) == 0 {
		fatal("%s has no `cases` mapping", *path)
	}

	if *list {
		for _, n := range names(d.Cases) {
			fmt.Println(n)
		}
		return
	}
	if *name == "" {
		fatal("--case is required unless --list")
	}

	c, ok := d.Cases[*name]
	if !ok {
		fatal("no test case %q in %s. Known: %s", *name, *path, strings.Join(names(d.Cases), ", "))
	}
	// A case with no keys is a valid case, and marshals to "null" from a nil
	// map — an empty object is what a reader expects.
	if c == nil {
		c = map[string]any{}
	}
	out, err := json.Marshal(c)
	if err != nil {
		fatal("cannot encode case %q: %v", *name, err)
	}
	fmt.Println(string(out))
}

func names(cases map[string]map[string]any) []string {
	out := make([]string, 0, len(cases))
	for n := range cases {
		out = append(out, n)
	}
	sort.Strings(out)
	return out
}

func fatal(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "testcase-helper: "+format+"\n", args...)
	os.Exit(1)
}
