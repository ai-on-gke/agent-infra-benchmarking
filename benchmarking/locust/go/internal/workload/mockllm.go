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

package workload

// The prompt and reply of mock-llm's scripted agent loop
// (workloads/images/mock-llm/mock_llm.py), which the openclaw and hermes actors
// both run against.
const (
	mockLLMPrompt = "do something please"
	mockLLMReply  = "I have successfully executed the bash commands!"
)

// mockLLMPromptFor and mockLLMReplyFor carry the nonce in the form mock-llm
// reads it from the prompt and writes it into its last answer.
func mockLLMPromptFor(nonce string) string { return mockLLMPrompt + " (request " + nonce + ")" }

func mockLLMReplyFor(nonce string) string { return mockLLMReply + " (request " + nonce + ")" }
