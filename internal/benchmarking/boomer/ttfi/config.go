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
	"encoding/json"
	"flag"
	"fmt"
	"time"
)

// ttfiConfig is the TTFI-only subset of a run's settings. It lives in this
// package rather than in dynconfig because no other user class reads these
// values, which lets dynconfig stay byte-identical to substrate's copy.
type ttfiConfig struct {
	Timeout  time.Duration
	Template string // ActorTemplate the TTFIUser measures
	Workload string // Workload the TTFIUser drives; see workload.go
}

// ttfiPayload mirrors the TTFI keys inside the --config-json object. Both
// this and dynconfig's own payload decode that same object; dynconfig ignores
// keys it does not declare, so the worker keeps a single config flag.
type ttfiPayload struct {
	Timeout  *float64 `json:"ttfi_timeout"`
	Template *string  `json:"ttfi_template"`
	Workload *string  `json:"ttfi_workload"`
}

func (c ttfiConfig) validate() error {
	if c.Timeout < 0 {
		return fmt.Errorf("ttfi_timeout cannot be negative: %v", c.Timeout)
	}
	return nil
}

// parseTTFIConfig decodes the TTFI keys out of a --config-json blob. Unset
// keys keep their zero value, which each reader resolves to its own default.
func parseTTFIConfig(jsonBytes []byte) (ttfiConfig, error) {
	var c ttfiConfig
	if len(jsonBytes) == 0 {
		return c, nil
	}
	var p ttfiPayload
	if err := json.Unmarshal(jsonBytes, &p); err != nil {
		return ttfiConfig{}, fmt.Errorf("decode config json: %w", err)
	}
	if p.Timeout != nil {
		c.Timeout = time.Duration(*p.Timeout * float64(time.Second))
	}
	if p.Template != nil {
		c.Template = *p.Template
	}
	// Left unchecked against the registry here: parsing reports what the blob
	// said, and resolveWorkload is where an unknown name becomes fatal, with
	// the registered names to hand for the message.
	if p.Workload != nil {
		c.Workload = *p.Workload
	}
	if err := c.validate(); err != nil {
		return ttfiConfig{}, fmt.Errorf("validate config: %w", err)
	}
	return c, nil
}

// workloadFlag registers --ttfi-workload on the worker's command line. It is
// declared by this package rather than by main for the same reason the
// --config-json lookup below is: main hands every user class the same
// userclass.Config and never learns which one has settings of its own, so a
// TTFI flag declared there would be a flag main has to know about. Package
// initialization runs before main's flag.Parse, so blank-importing this
// package is enough to make the flag exist.
//
// The known names are deliberately not listed here: the registry is still
// filling up while this var is initialized. resolveWorkload lists them when a
// name does not match, which is when they are actually wanted.
var workloadFlag = flag.String("ttfi-workload", "",
	"TTFI only: the workload a TTFIUser drives. Overrides ttfi_workload in --config-json; "+
		"unset selects "+defaultTTFIWorkload+".")

// loadTTFIConfig reads the TTFI settings off the worker's command line. The
// --config-json flag is looked up by name rather than taken as a parameter so
// that main stays generic over user classes. Must run after flag.Parse().
func loadTTFIConfig() (ttfiConfig, error) {
	var jsonBytes []byte
	if f := flag.Lookup("config-json"); f != nil {
		jsonBytes = []byte(f.Value.String())
	}
	return resolveTTFIConfig(jsonBytes, *workloadFlag)
}

// resolveTTFIConfig decodes the --config-json blob and lets the dedicated
// flags override what it said. The flag wins because it is the more specific
// of the two: one --config-json object carries every user class's keys and
// tends to be set once for a whole deployment, while --ttfi-workload is typed
// for the worker in front of you.
func resolveTTFIConfig(jsonBytes []byte, workload string) (ttfiConfig, error) {
	c, err := parseTTFIConfig(jsonBytes)
	if err != nil {
		return ttfiConfig{}, err
	}
	if workload != "" {
		c.Workload = workload
	}
	return c, nil
}
