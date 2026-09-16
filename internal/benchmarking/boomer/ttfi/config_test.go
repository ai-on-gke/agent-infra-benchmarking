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

package ttfi

import (
	"flag"
	"testing"
	"time"

	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/dynconfig"
)

// A worker is given one --config-json object holding every user class's keys,
// so the blob under test carries dynconfig's alongside TTFI's.
const configBlob = `{
	"trace_probability": 0.5,
	"min_wait_time": 0.1,
	"max_wait_time": 0.5,
	"resume_mode": "explicit",
	"ttfi_timeout": 30.0,
	"ttfi_template": "glutton",
	"ttfi_workload": "glutton"
}`

func TestParseTTFIConfig(t *testing.T) {
	cfg, err := parseTTFIConfig([]byte(configBlob))
	if err != nil {
		t.Fatalf("parseTTFIConfig failed: %v", err)
	}

	if cfg.Timeout != 30*time.Second {
		t.Errorf("Timeout: got %v, want 30s", cfg.Timeout)
	}
	if cfg.Template != "glutton" {
		t.Errorf("Template: got %q, want glutton", cfg.Template)
	}
	if cfg.Workload != "glutton" {
		t.Errorf("Workload: got %q, want glutton", cfg.Workload)
	}
}

// An absent blob is the common case — the worker runs on built-in defaults —
// and must not be an error.
func TestParseTTFIConfigEmpty(t *testing.T) {
	cfg, err := parseTTFIConfig(nil)
	if err != nil {
		t.Fatalf("parseTTFIConfig(nil) failed: %v", err)
	}
	if cfg != (ttfiConfig{}) {
		t.Errorf("empty blob: got %+v, want zero config", cfg)
	}
}

func TestParseTTFIConfigInvalidValues(t *testing.T) {
	tests := []struct {
		name string
		json string
	}{
		{
			name: "negative ttfi timeout",
			json: `{"ttfi_timeout": -1.0}`,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if _, err := parseTTFIConfig([]byte(tt.json)); err == nil {
				t.Errorf("expected parseTTFIConfig to fail for %s, got nil error", tt.name)
			}
		})
	}
}

// The flag has to exist on the worker's command line for --ttfi-workload to be
// accepted at all, and nothing but importing this package puts it there.
func TestWorkloadFlagIsRegistered(t *testing.T) {
	f := flag.Lookup("ttfi-workload")
	if f == nil {
		t.Fatal("--ttfi-workload is not registered; the worker would reject it as an unknown flag")
	}
	// Empty is what lets --config-json and then the built-in default still
	// have a say; a non-empty default would silently outrank both.
	if f.DefValue != "" {
		t.Errorf("default: got %q, want empty", f.DefValue)
	}
}

func TestResolveTTFIConfigWorkloadPrecedence(t *testing.T) {
	tests := []struct {
		name string
		json string
		flag string
		want string
	}{
		{
			name: "flag alone",
			json: `{}`,
			flag: "from-flag",
			want: "from-flag",
		},
		{
			name: "config json alone",
			json: `{"ttfi_workload": "from-json"}`,
			want: "from-json",
		},
		{
			name: "flag overrides config json",
			json: `{"ttfi_workload": "from-json"}`,
			flag: "from-flag",
			want: "from-flag",
		},
		{
			// Neither said anything, so the choice is left to
			// resolveWorkload's default rather than made here.
			name: "neither",
			json: `{}`,
			want: "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfg, err := resolveTTFIConfig([]byte(tt.json), tt.flag)
			if err != nil {
				t.Fatalf("resolveTTFIConfig failed: %v", err)
			}
			if cfg.Workload != tt.want {
				t.Errorf("Workload: got %q, want %q", cfg.Workload, tt.want)
			}
		})
	}
}

// Overriding the workload must not disturb the keys that share the blob with
// it, or selecting a workload would quietly reset the run's other settings.
func TestResolveTTFIConfigKeepsTheOtherKeys(t *testing.T) {
	cfg, err := resolveTTFIConfig([]byte(configBlob), "from-flag")
	if err != nil {
		t.Fatalf("resolveTTFIConfig failed: %v", err)
	}
	if cfg.Workload != "from-flag" {
		t.Errorf("Workload: got %q, want from-flag", cfg.Workload)
	}
	if cfg.Timeout != 30*time.Second {
		t.Errorf("Timeout: got %v, want 30s", cfg.Timeout)
	}
	if cfg.Template != "glutton" {
		t.Errorf("Template: got %q, want glutton", cfg.Template)
	}
}

// The TTFI keys and dynconfig's share one JSON object, each parser ignoring
// the other's fields. That only holds while neither rejects unknown keys, so
// assert it here rather than discovering it from a worker that exits at boot.
func TestTTFIKeysCoexistWithDynconfig(t *testing.T) {
	dynCfg, err := dynconfig.Parse([]byte(configBlob), dynconfig.Config{})
	if err != nil {
		t.Fatalf("dynconfig rejected a blob carrying TTFI keys: %v", err)
	}
	if dynCfg.ResumeMode != dynconfig.ResumeModeExplicit {
		t.Errorf("ResumeMode: got %q, want %q", dynCfg.ResumeMode, dynconfig.ResumeModeExplicit)
	}

	ttfiCfg, err := parseTTFIConfig([]byte(configBlob))
	if err != nil {
		t.Fatalf("parseTTFIConfig rejected a blob carrying dynconfig keys: %v", err)
	}
	if ttfiCfg.Template != "glutton" {
		t.Errorf("Template: got %q, want glutton", ttfiCfg.Template)
	}
}
