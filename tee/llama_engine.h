// Copyright 2026 Google LLC.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#ifndef ZTAB_SERVER_LLAMA_ENGINE_H_
#define ZTAB_SERVER_LLAMA_ENGINE_H_

#include <memory>
#include <string>

#include "absl/status/statusor.h"

namespace ztab {

// A simple LLM inference engine wrapping llama.cpp. Loads a GGUF model
// and generates text from a prompt.
//
// This is intentionally minimal — just enough to verify the LLM pipeline
// works inside the TEE. Proper session management and multi-turn support
// will come later.
class LlamaEngine {
 public:
  virtual ~LlamaEngine() = default;

  // Generates text from the given prompt. Returns the generated text
  // (not including the prompt itself).
  virtual absl::StatusOr<std::string> Generate(const std::string& prompt,
                                               int max_tokens = 256) = 0;
};

// Creates a LlamaEngine by loading a GGUF model from the given path.
//
// Args:
//   model_path: Path to a .gguf model file.
//   gpu_layers: Number of layers to offload to GPU (0 = CPU only,
//               999 = all layers for full GPU offload on H100).
//   n_ctx:      Context window size in tokens. If 0 (default), reads the
//               model's training context size from its metadata.
absl::StatusOr<std::unique_ptr<LlamaEngine>> CreateLlamaEngine(
    const std::string& model_path, int gpu_layers = 0, int n_ctx = 0);

}  // namespace ztab

#endif  // ZTAB_SERVER_LLAMA_ENGINE_H_
