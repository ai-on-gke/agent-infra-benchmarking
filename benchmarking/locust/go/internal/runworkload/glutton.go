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

package runworkload

import (
	"context"
	"fmt"
	"net/http"

	gluttonpb "github.com/agent-substrate/substrate/internal/proto/glutton"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

// Glutton's own surface, mirrored from the glutton benchmark rather than
// imported from it: neither the route nor its encoding belongs to either
// benchmark's load generator, so neither should have to depend on the other to
// reach them.
const (
	gluttonWorkloadName = "glutton"

	// The ActorTemplate scenario_runner/src/scenario_runner/workloads/ creates
	// for glutton.
	gluttonTemplate = "glutton"

	// glutton's HTTP routes (internal/benchmarking/glutton in the submodule).
	pingPath         = "/ping"
	writeRAMPath     = "/writeram"
	writeDiskPath    = "/writedisk"
	protoContentType = "application/x-protobuf"

	// The memory array and file a cycle's load rewrites, the same each time so
	// the actor's footprint stays at one of each.
	loadKey = "load"
)

func init() {
	registerWorkload(gluttonWorkload{})
}

// gluttonWorkload drives substrate's glutton actor. Its /ping route echoes the
// message it was sent, and that echo is what makes glutton usable here: it
// distinguishes a sandbox that ran the instruction from one that merely
// accepted the connection, which is the line a measurement window closes on.
type gluttonWorkload struct{}

func (gluttonWorkload) Name() string { return gluttonWorkloadName }

func (gluttonWorkload) DefaultTemplate() string { return gluttonTemplate }

// SendInstruction POSTs one Ping and verifies the echo. A wrong echo is a
// fault rather than a symptom of activation, so it is reported as
// unretryable even though the actor answered.
func (gluttonWorkload) SendInstruction(ctx context.Context, actor actorRef) (bool, error) {
	message := uuid.NewString()
	body, err := proto.Marshal(&gluttonpb.PingRequest{Message: message})
	if err != nil {
		return false, err
	}

	return sendHTTPInstruction(ctx, actor, httpInstruction{
		Method:      http.MethodPost,
		Path:        pingPath,
		ContentType: protoContentType,
		Body:        body,
		Verify: func(respBody []byte) error {
			pong := &gluttonpb.PingResponse{}
			if err := proto.Unmarshal(respBody, pong); err != nil {
				return err
			}
			if pong.GetMessage() != message {
				return fmt.Errorf("ping echo mismatch: sent=%q recv=%q", message, pong.GetMessage())
			}
			return nil
		},
	})
}

// WriteRAM rewrites size of the actor's memory. The first call on an actor
// allocates and fills the array, which rotating writes need to exist; later
// ones move on through it rather than dirtying the same pages again.
func (gluttonWorkload) WriteRAM(ctx context.Context, actor actorRef, size string, first bool) error {
	mode := gluttonpb.WriteMode_WRITE_MODE_OVERWRITE_ROTATE
	if first {
		mode = gluttonpb.WriteMode_WRITE_MODE_TRUNCATE
	}
	body, err := proto.Marshal(&gluttonpb.WriteRAMRequest{
		Key:       loadKey,
		Size:      size,
		WriteMode: mode,
	})
	if err != nil {
		return err
	}
	_, err = sendHTTPInstruction(ctx, actor, httpInstruction{
		Method:      http.MethodPost,
		Path:        writeRAMPath,
		ContentType: protoContentType,
		Body:        body,
	})
	return err
}

// WriteDisk writes size new random bytes over the actor's file.
func (gluttonWorkload) WriteDisk(ctx context.Context, actor actorRef, size int) error {
	body, err := proto.Marshal(&gluttonpb.WriteDiskRequest{
		Key:       loadKey,
		Size:      int32(size),
		WriteMode: gluttonpb.WriteMode_WRITE_MODE_TRUNCATE,
	})
	if err != nil {
		return err
	}
	_, err = sendHTTPInstruction(ctx, actor, httpInstruction{
		Method:      http.MethodPost,
		Path:        writeDiskPath,
		ContentType: protoContentType,
		Body:        body,
		Verify: func(respBody []byte) error {
			written := &gluttonpb.WriteDiskResponse{}
			if err := proto.Unmarshal(respBody, written); err != nil {
				return err
			}
			if written.GetSize() != int64(size) {
				return fmt.Errorf("wrote %d bytes, file is %d", size, written.GetSize())
			}
			return nil
		},
	})
	return err
}
