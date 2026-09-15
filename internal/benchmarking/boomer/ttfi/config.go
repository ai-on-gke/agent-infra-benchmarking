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
}

// ttfiPayload mirrors the TTFI keys inside the --config-json object. Both
// this and dynconfig's own payload decode that same object; dynconfig ignores
// keys it does not declare, so the worker keeps a single config flag.
type ttfiPayload struct {
	Timeout  *float64 `json:"ttfi_timeout"`
	Template *string  `json:"ttfi_template"`
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
	if err := c.validate(); err != nil {
		return ttfiConfig{}, fmt.Errorf("validate config: %w", err)
	}
	return c, nil
}

// loadTTFIConfig reads the TTFI keys from the worker's shared --config-json
// flag. Looked up by name rather than taken as a parameter so that main stays
// generic over user classes: it hands every class the same userclass.Config
// and never learns which one needs TTFI settings. Must run after flag.Parse().
func loadTTFIConfig() (ttfiConfig, error) {
	f := flag.Lookup("config-json")
	if f == nil {
		return ttfiConfig{}, nil
	}
	return parseTTFIConfig([]byte(f.Value.String()))
}
