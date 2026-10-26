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

// ControlContext bounds one control-plane call like Context, but lets it
// finish when the run ends rather than cutting it off. ateapi runs a call's
// workflow on the call's own context, so cancelling a suspend mid-flight kills
// its checkpoint and crashes the actor, and a cancelled resume leaves it
// RESUMING; the teardown that follows could then only delete it from any
// state. The call still ends at the timeout, inside the teardown's drain, and
// with ctx when ctx ends for any other reason, such as the teardown's budget.
func (r Recorder) ControlContext(ctx context.Context) (context.Context, context.CancelFunc) {
	callCtx, cancelTimeout := r.Context(context.WithoutCancel(ctx))
	callCtx, cancel := context.WithCancelCause(callCtx)
	stop := context.AfterFunc(ctx, func() {
		if cause := context.Cause(ctx); !errors.Is(cause, ErrRunEnded) {
			cancel(cause)
		}
	})
	return callCtx, func() {
		stop()
		cancel(nil)
		cancelTimeout()
	}
}

// GRPC wraps a unary gRPC call with a span and its row. The latency reported
// is the server's own elapsed trailer when it sent one, and client wall clock
// otherwise. The call is let finish when the run ends; see ControlContext.
//
// A call refused with Aborted is retried: it is a lost race with another
// operation on the same object, which clears once that lands, and upstream's
// router retries it the same way.
func (r Recorder) GRPC(ctx context.Context, name string, do func(context.Context, *metadata.MD) error) error {
	ctx, span := r.Tracer.Start(ctx, name)
	defer span.End()
	// ctx stays the run's, which CutOff reads; callCtx no longer ends with it.
	callCtx, cancel := r.ControlContext(ctx)
	defer cancel()

	return r.reportGRPC(ctx, span, name, attemptGRPC(callCtx, do))
}

// GRPCWaiting is GRPC for a call the server refuses while it has no room for
// it yet, which waitable tells apart from other errors: refused that way, the
// call is made again, backing off, until it goes through or the timeout runs
// out, and stops waiting when the run ends. The row is the last attempt's
// alone.
//
// A call refused at first also reports the wait as the window waitName, from
// the first attempt's start to the last one's, or as a failure over the time
// spent when it ran out.
func (r Recorder) GRPCWaiting(ctx context.Context, name, waitName string, waitable func(error) bool, do func(context.Context, *metadata.MD) error) error {
	ctx, span := r.Tracer.Start(ctx, name)
	defer span.End()
	callCtx, cancel := r.ControlContext(ctx)
	defer cancel()
	// The calls are let finish when the run ends, but the waits between them
	// are not.
	waitCtx, cancelWait := r.Context(ctx)
	defer cancelWait()

	start := time.Now()
	var last attempt
	var lastStart time.Time
	attempts, _ := retry(waitCtx, r.Timeout, waitable, func() error {
		lastStart = time.Now()
		last = attemptGRPC(callCtx, do)
		return last.err
	})
	if attempts > 1 {
		wait, err := lastStart.Sub(start), error(nil)
		if waitable(last.err) {
			wait, err = time.Since(start), last.err
		}
		_, waitSpan := r.Tracer.Start(ctx, waitName, trace.WithTimestamp(start))
		r.Window(ctx, waitSpan, waitName, wait, err)
		waitSpan.End(trace.WithTimestamp(start.Add(wait)))
	}
	return r.reportGRPC(ctx, span, name, last)
}

// attempt is one gRPC call as its row reports it: the trailer of its last
// try, how many tries it took, and its client wall clock over all of them.
type attempt struct {
	tr      metadata.MD
	tries   int
	latency time.Duration
	err     error
}

// attemptGRPC makes the call, again while it is refused with Aborted.
func attemptGRPC(callCtx context.Context, do func(context.Context, *metadata.MD) error) attempt {
	start := time.Now()
	var a attempt
	a.tries, a.err = retry(callCtx, RetryBudget, func(err error) bool { return status.Code(err) == codes.Aborted }, func() error {
		a.tr = nil
		return do(callCtx, &a.tr)
	})
	a.latency = time.Since(start)
	return a
}

// reportGRPC reports a as name's row under span.
func (r Recorder) reportGRPC(ctx context.Context, span trace.Span, name string, a attempt) error {
	latency, source := boomerutil.ElapsedFromMD(a.tr, ateinterceptors.ServerElapsedTrailer, a.latency)
	// The server's elapsed trailer is the last try's alone, and would hide the
	// conflicts the client waited through before it.
	if a.tries > 1 {
		latency, source = a.latency, boomerutil.SourceClient
	}
	if source == boomerutil.SourceServer {
		span.SetAttributes(attribute.Float64("server.elapsed_ms", boomerutil.MsFloat(latency)))
	}
	if CutOff(ctx, a.err) {
		return a.err
	}
	boomerutil.LogSampledTrace(span, name, latency, source, a.err)
	if a.err != nil {
		recordFailure("grpc", name, r.UserClass, latency, a.err.Error())
		return a.err
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
	_, err := retry(ctx, RetryBudget, retryable, func() error { return do(ctx) })
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
