# e2b-spike

Phase-1 validation harness for the E2B benchmark integration. It is **not** the
benchmark worker — it is a standalone, single-shot program that proves our
hand-written E2B clients speak the wire correctly before any of this is wired
into the boomer/Locust worker.

It drives one pass through the sandbox lifecycle and prints the latency of each
operation:

```
CreateSandbox  →  Exec  →  PauseSandbox  →  ResumeSandbox  →  ExecAfterResume  →  KillSandbox
```

- **Control plane** (create / pause / resume / kill) goes through the E2B REST
  API via `internal/benchmarking/e2b/control`.
- **Data plane** (the `Exec` instruction) goes through the in-sandbox envd
  daemon over ConnectRPC via `internal/benchmarking/e2b/envd`, using the client
  generated from E2B's envd protos under `internal/benchmarking/e2b/envdpb`.

These two map onto the same control-plus-instruction split the substrate
boomer-worker already uses (`ateapi` gRPC stub + atenet router).

## Run

```sh
export E2B_API_KEY=<your E2B API key>
go run ./cmd/benchmarking/e2b-spike/
```

Against E2B Cloud the defaults (`--domain e2b.app`, `--template base`) should
just work. For a self-hosted E2B deployment, point it at your domain:

```sh
go run ./cmd/benchmarking/e2b-spike/ --domain <your-e2b-domain> --template <template-id>
```

### Flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `--domain` | `e2b.app` | E2B base domain. API host is `https://api.<domain>`; sandboxes serve at `<port>-<id>.<domain>`. |
| `--template` | `base` | E2B template ID to create the sandbox from. |
| `--timeout` | `300` | Sandbox TTL in seconds, applied on create and resume. |
| `--command` | `echo hello-from-e2b` | Instruction run inside the sandbox via `/bin/sh -c`. |
| `--op-timeout` | `2m` | Per-operation context timeout. |

`E2B_API_KEY` is read from the environment and is required.

## Notes

- `pause`/`resume` may be unavailable on some deployments. If `pause` or
  `resume` fails the spike logs a warning, skips the rest of the pause/resume
  path, and still kills the sandbox — so you always get create/exec/kill
  timings.
- On any failure the sandbox is still killed (best-effort) so a failed run does
  not leak a billable sandbox.

## Regenerating the envd client

The generated code under `internal/benchmarking/e2b/envdpb/` is produced from
the vendored protos in `internal/benchmarking/e2b/spec/` (copied from
`e2b-dev/infra` `packages/envd/spec/`). To regenerate:

```sh
go install github.com/bufbuild/buf/cmd/buf@latest
go install google.golang.org/protobuf/cmd/protoc-gen-go@latest
go install connectrpc.com/connect/cmd/protoc-gen-connect-go@latest
cd internal/benchmarking/e2b/spec && buf generate
```
