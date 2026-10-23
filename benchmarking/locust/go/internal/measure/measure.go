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

// Package measure times the calls a user class makes and reports each as a
// span and a row: to boomer, which sends it to the locust master, and to
// Prometheus. It knows nothing about what the calls do, so every user class
// reports its rows the same way.
package measure

import (
	"context"
	"errors"
	"time"

	"github.com/agent-substrate/substrate/internal/ateinterceptors"
	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/boomerutil"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// ErrRunEnded is the cause the run's context is cancelled with when the
// teardown starts.
var ErrRunEnded = errors.New("the run ended")

// CutOff reports whether err is a call the end of the run cut off rather than
// a result. locust records nothing for a user it stops mid-request either; an
// instruction the run's last seconds interrupt says nothing about the system.
func CutOff(ctx context.Context, err error) bool {
	return err != nil && errors.Is(context.Cause(ctx), ErrRunEnded)
}

// Recorder reports one user's calls. Cheap to copy, so a user can build one
// per call from its own fields.
type Recorder struct {
	Tracer trace.Tracer

	// UserClass is the locust class name the rows are reported under.
	UserClass string

	// Timeout bounds each call, retries included; zero leaves it unbounded.
	Timeout time.Duration

	// WindowMethod is the transport label of the rows Window reports. A
	// window spans gRPC calls and HTTP instructions, so neither transport
	// label describes it.
	WindowMethod string
}

// Context bounds one call, retries included, by the recorder's timeout.
func (r Recorder) Context(ctx context.Context) (context.Context, context.CancelFunc) {
	if r.Timeout <= 0 {
		return ctx, func() {}
	}
	return context.WithTimeout(ctx, r.Timeout)
}

// GRPC wraps a unary gRPC call with a span and its row. The latency reported
// is the server's own elapsed trailer when it sent one, and client wall clock
// otherwise.
//
// A call refused with Aborted is retried: it is a lost race with another
// operation on the same object, which clears once that lands, and upstream's
// router retries it the same way.
func (r Recorder) GRPC(ctx context.Context, name string, do func(context.Context, *metadata.MD) error) error {
	ctx, span := r.Tracer.Start(ctx, name)
	defer span.End()
	ctx, cancel := r.Context(ctx)
	defer cancel()

	start := time.Now()
	var tr metadata.MD
	attempts, err := retry(ctx, func(err error) bool { return status.Code(err) == codes.Aborted }, func() error {
		tr = nil
		return do(ctx, &tr)
	})
	clientLatency := time.Since(start)

	latency, source := boomerutil.ElapsedFromMD(tr, ateinterceptors.ServerElapsedTrailer, clientLatency)
	// The server's elapsed trailer is the last attempt's alone, and would hide
	// the conflicts the client waited through before it.
	if attempts > 1 {
		latency, source = clientLatency, boomerutil.SourceClient
	}
	if source == boomerutil.SourceServer {
		span.SetAttributes(attribute.Float64("server.elapsed_ms", boomerutil.MsFloat(latency)))
	}
	if CutOff(ctx, err) {
		return err
	}
	boomerutil.LogSampledTrace(span, name, latency, source, err)
	if err != nil {
		recordFailure("grpc", name, r.UserClass, latency, err.Error())
		return err
	}
	recordSuccess("grpc", name, r.UserClass, latency)
	return nil
}

// HTTP times one HTTP call as the row name, by client wall clock. It is retried
// while retryable accepts its error; nil retries nothing.
func (r Recorder) HTTP(ctx context.Context, name string, retryable func(error) bool, do func(context.Context) error) error {
	ctx, span := r.Tracer.Start(ctx, name)
	defer span.End()
	ctx, cancel := r.Context(ctx)
	defer cancel()

	if retryable == nil {
		retryable = func(error) bool { return false }
	}
	start := time.Now()
	_, err := retry(ctx, retryable, func() error { return do(ctx) })
	latency := time.Since(start)

	if CutOff(ctx, err) {
		return err
	}
	boomerutil.LogSampledTrace(span, name, latency, boomerutil.SourceClient, err)
	if err != nil {
		recordFailure("http", name, r.UserClass, latency, err.Error())
		return err
	}
	recordSuccess("http", name, r.UserClass, latency)
	return nil
}

// Window closes out a window the caller timed over several calls under span,
// reported as name: a success, or a failure over the wait it spent.
func (r Recorder) Window(ctx context.Context, span trace.Span, name string, total time.Duration, err error) {
	if CutOff(ctx, err) {
		return
	}
	span.SetAttributes(attribute.Float64("total_ms", boomerutil.MsFloat(total)))
	boomerutil.LogSampledTrace(span, name, total, boomerutil.SourceClient, err)
	if err != nil {
		recordFailure(r.WindowMethod, name, r.UserClass, total, err.Error())
		return
	}
	recordSuccess(r.WindowMethod, name, r.UserClass, total)
}
