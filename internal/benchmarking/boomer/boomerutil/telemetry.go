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

package boomerutil

import (
	"log/slog"
	"time"

	"go.opentelemetry.io/otel/trace"
)

const (
	SourceClient = "client"
	SourceServer = "server"
)

// MsFloat converts a duration to a float64 representing milliseconds.
func MsFloat(d time.Duration) float64 {
	return float64(d.Nanoseconds()) / 1e6
}

// LogSampledTrace emits a single structured line per sampled span. Operators
// parse these lines (e.g. via fluentbit) to rebuild the trace stream.
func LogSampledTrace(span trace.Span, name string, latency time.Duration, source string, err error) {
	sc := span.SpanContext()
	if !sc.IsSampled() {
		return
	}
	attrs := []any{
		slog.String("name", name),
		slog.String("trace_id", sc.TraceID().String()),
		slog.Float64("duration_ms", MsFloat(latency)),
		slog.String("source", source),
	}
	if err != nil {
		attrs = append(attrs, slog.String("err", err.Error()))
		slog.Info("traced span (failed)", attrs...)
		return
	}
	slog.Info("traced span", attrs...)
}
