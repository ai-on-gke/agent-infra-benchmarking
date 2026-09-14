package openclaw

import (
	"context"
	"net/http"
	"time"

	bmetrics "github.com/agent-substrate/substrate/internal/benchmarking/boomer/metrics"
	"github.com/agent-substrate/substrate/internal/benchmarking/boomer/userclass"
	"github.com/agent-substrate/substrate/pkg/proto/ateapipb"
	"github.com/google/uuid"
)

func init() {
	userclass.Add(userclass.Entry{
		Name:       "openclaw",
		LocustFile: "openclaw.py",
		UserClass:  "OpenClawUser",
		Init:       initOpenClaw,
	})
}

func initOpenClaw(cfg *userclass.Config) (func(), func(context.Context)) {
	actorName := "openclaw-" + uuid.NewString()

	_, err := cfg.APIStub.CreateActor(context.Background(), &ateapipb.CreateActorRequest{
		Actor: &ateapipb.Actor{
			Metadata:      &ateapipb.ResourceMetadata{Atespace: cfg.Atespace, Name: actorName},
			ActorTemplate: &ateapipb.ObjectRef{Atespace: "benchmark-workloads", Name: "openclaw"},
		},
	})
	if err != nil {
		panic(err)
	}

	_, err = cfg.APIStub.ResumeActor(context.Background(), &ateapipb.ResumeActorRequest{
		Actor: &ateapipb.ObjectRef{Atespace: cfg.Atespace, Name: actorName},
	})
	if err != nil {
		panic(err)
	}

	taskFn := func() {
		start := time.Now()

		req, _ := http.NewRequest(http.MethodPost, cfg.RouterURL+"/invoke", nil)
		req.Host = actorName + "." + cfg.Atespace + ".actors.resources.substrate.ate.dev"

		resp, err := cfg.HTTPClient.Do(req)
		latency := time.Since(start)

		if err != nil || resp.StatusCode != 200 {
			bmetrics.RecordFailure("http", "OpenClawInvoke", "OpenClawUser", latency, "failed")
			return
		}
		bmetrics.RecordSuccess("http", "OpenClawInvoke", "OpenClawUser", latency, 0)
	}

	shutdownFn := func(ctx context.Context) {
		cfg.APIStub.SuspendActor(ctx, &ateapipb.SuspendActorRequest{Actor: &ateapipb.ObjectRef{Atespace: cfg.Atespace, Name: actorName}})
		cfg.APIStub.DeleteActor(ctx, &ateapipb.DeleteActorRequest{Actor: &ateapipb.ObjectRef{Atespace: cfg.Atespace, Name: actorName}})
	}

	return taskFn, shutdownFn
}
