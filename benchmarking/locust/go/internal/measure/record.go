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

package measure

import (
	"time"

	bmetrics "github.com/agent-substrate/substrate/internal/benchmarking/boomer/metrics"
)

// recordSuccess and recordFailure report a row to boomer and Prometheus.
// Boomer takes whole milliseconds and truncates to them, which would bias
// every row down by half a millisecond on average and turn a 0.9 ms call into
// 0 ms; the latency is rounded to the nearest millisecond first.
func recordSuccess(method, name, userClass string, latency time.Duration) {
	bmetrics.RecordSuccess(method, name, userClass, latency.Round(time.Millisecond), 0)
}

func recordFailure(method, name, userClass string, latency time.Duration, errMsg string) {
	bmetrics.RecordFailure(method, name, userClass, latency.Round(time.Millisecond), errMsg)
}
