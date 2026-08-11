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

// Simplified llama.cpp engine for ZTAB. Based on
// confidential-federated-compute/containers/gcp/llama_cpp_batched_inference_engine.cc
// but stripped down to single-prompt, synchronous generation.

#include "llama_engine.h"

#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "absl/log/log.h"
#include "absl/status/status.h"
#include "absl/status/statusor.h"
#include "absl/strings/str_cat.h"
#include "absl/strings/str_format.h"
#include "absl/time/clock.h"
#include "absl/time/time.h"
#include "include/llama.h"

namespace ztab {
namespace {

constexpr int kBatchSize = 2048;

// Chat template markers for instruction-tuned Gemma models.
// TODO: Consider extracting these to configuration or taking them via the
// session_manager API to support other models (Llama 3, Mistral) in the future.
// Design Decision: We are intentionally focused strictly on the Gemma model
// family for this milestone to avoid scope creep.
constexpr char kStartOfTurn[] = "<start_of_turn>";
constexpr char kEndOfTurn[] = "<end_of_turn>";

// Forward llama.cpp logs to Abseil.
static void LlamaLogger(ggml_log_level level, const char* text,
                        void* user_data) {
  std::string msg(text);
  if (!msg.empty() && msg.back() == '\n') {
    msg.pop_back();
  }
  if (msg.empty()) return;

  if (level == GGML_LOG_LEVEL_ERROR) {
    LOG(ERROR) << "llama.cpp: " << msg;
  } else if (level == GGML_LOG_LEVEL_WARN) {
    LOG(WARNING) << "llama.cpp: " << msg;
  } else {
    LOG(INFO) << "llama.cpp: " << msg;
  }
}

// Helper batch operations (from CFC).
void BatchClear(llama_batch& batch) { batch.n_tokens = 0; }

void BatchAdd(llama_batch& batch, llama_token token, llama_pos pos,
              const std::vector<llama_seq_id>& seq_ids, bool logits) {
  batch.token[batch.n_tokens] = token;
  batch.pos[batch.n_tokens] = pos;
  batch.n_seq_id[batch.n_tokens] = seq_ids.size();
  for (size_t i = 0; i < seq_ids.size(); ++i) {
    batch.seq_id[batch.n_tokens][i] = seq_ids[i];
  }
  batch.logits[batch.n_tokens] = logits ? 1 : 0;
  batch.n_tokens++;
}

class LlamaEngineImpl : public LlamaEngine {
 public:
  LlamaEngineImpl(llama_model* model, const llama_vocab* vocab, int n_ctx)
      : model_(model), vocab_(vocab), n_ctx_(n_ctx) {
    batch_ = llama_batch_init(kBatchSize, 0, 1);

    // Discover stop tokens from the model vocabulary.
    auto maybe_add_stop = [&](llama_token id) {
      if (id < 0) return;
      char buf[256];
      int n = llama_token_to_piece(vocab_, id, buf, sizeof(buf), 0, true);
      if (n > 0) {
        stop_strings_.emplace_back(buf, n);
      }
    };
    maybe_add_stop(llama_vocab_eot(vocab_));
    stop_strings_.push_back(kEndOfTurn);
    stop_strings_.push_back(kStartOfTurn);

    // Create context once — reused across Generate() calls. KV cache is
    // cleared per call instead of reallocating the entire context.
    llama_context_params ctx_params = llama_context_default_params();
    ctx_params.n_ctx = n_ctx_;
    ctx_params.n_batch = kBatchSize;
    ctx_params.n_ubatch = kBatchSize;
    ctx_params.flash_attn_type = LLAMA_FLASH_ATTN_TYPE_ENABLED;
    ctx_ = llama_init_from_model(model_, ctx_params);
    if (!ctx_) {
      LOG(ERROR) << "Failed to create llama context (n_ctx=" << n_ctx_ << ").";
    }

    // Create sampler (greedy for simplicity).
    sampler_ = llama_sampler_chain_init(llama_sampler_chain_default_params());
    llama_sampler_chain_add(sampler_, llama_sampler_init_greedy());
  }

  ~LlamaEngineImpl() override {
    if (sampler_) llama_sampler_free(sampler_);
    if (ctx_) llama_free(ctx_);
    llama_batch_free(batch_);
    if (model_) llama_model_free(model_);
  }

  absl::StatusOr<std::string> Generate(const std::string& prompt,
                                       int max_tokens) override {
    std::lock_guard<std::mutex> lock(generate_mu_);
    if (!ctx_) {
      return absl::InternalError("Llama context not initialized.");
    }

    // Apply chat template.
    std::string formatted =
        absl::StrCat(kStartOfTurn, "user\n", prompt, kEndOfTurn, "\n",
                     kStartOfTurn, "model\n");

    // Tokenize.
    std::vector<llama_token> tokens(n_ctx_);
    int n_tokens = llama_tokenize(vocab_, formatted.c_str(), formatted.size(),
                                  tokens.data(), tokens.size(), true, true);
    if (n_tokens < 0) {
      return absl::InvalidArgumentError(absl::StrCat(
          "Prompt exceeds context length (Tokenization failed, n_tokens=",
          n_tokens, ")"));
    }
    tokens.resize(n_tokens);

    LOG(INFO) << "Prompt tokenized to " << n_tokens << " tokens.";

    // Clear KV cache and sampler state for the new prompt (reuse the context).
    // Note: Static analysis tools may complain that this should be
    // `llama_kv_cache_clear`. This is intentional. We are pinned to llama.cpp
    // commit 98d2d288 where the API is named `llama_memory_clear`. Do not
    // change this unless bumping the pin.
    llama_memory_clear(llama_get_memory(ctx_), /* data= */ true);
    llama_sampler_reset(sampler_);

    // Fill batch with prompt tokens.
    BatchClear(batch_);
    for (int i = 0; i < n_tokens; i++) {
      BatchAdd(batch_, tokens[i], i, {0}, i == n_tokens - 1);
    }

    // Process prompt.
    if (llama_decode(ctx_, batch_) != 0) {
      return absl::InternalError("llama_decode failed on prompt.");
    }

    // Generate tokens.
    std::string output;
    int generated_token_count = 0;
    absl::Time decode_start = absl::Now();
    for (int i = 0; i < max_tokens; i++) {
      llama_token new_token = llama_sampler_sample(sampler_, ctx_, -1);

      // Check for EOS.
      if (llama_vocab_is_eog(vocab_, new_token)) {
        break;
      }
      generated_token_count++;

      // Convert token to text.
      char buf[256];
      int n =
          llama_token_to_piece(vocab_, new_token, buf, sizeof(buf), 0, true);
      if (n > 0) {
        output.append(buf, n);
      }

      // Check stop strings.
      bool should_stop = false;
      for (const auto& stop : stop_strings_) {
        if (output.size() >= stop.size() &&
            output.compare(output.size() - stop.size(), stop.size(), stop) ==
                0) {
          output.resize(output.size() - stop.size());
          should_stop = true;
          break;
        }
      }
      if (should_stop) break;

      // Prepare next batch.
      BatchClear(batch_);
      BatchAdd(batch_, new_token, n_tokens + i, {0}, true);
      if (llama_decode(ctx_, batch_) != 0) {
        return absl::InternalError(
            absl::StrCat("llama_decode failed at token ", i));
      }
    }

    // Trim trailing whitespace.
    while (!output.empty() && (output.back() == '\n' || output.back() == ' ')) {
      output.pop_back();
    }

    absl::Duration decode_elapsed = absl::Now() - decode_start;
    double decode_secs = absl::ToDoubleSeconds(decode_elapsed);
    double tokens_per_sec =
        (decode_secs > 0) ? generated_token_count / decode_secs : 0.0;
    LOG(INFO) << "Generated " << generated_token_count << " tokens ("
              << output.size() << " bytes) in "
              << absl::FormatDuration(decode_elapsed) << " ("
              << absl::StrFormat("%.2f", tokens_per_sec) << " tok/s)";
    return output;
  }

 private:
  llama_model* model_;
  const llama_vocab* vocab_;
  int n_ctx_;
  llama_context* ctx_ = nullptr;
  llama_sampler* sampler_ = nullptr;
  llama_batch batch_;
  std::vector<std::string> stop_strings_;
  std::mutex generate_mu_;
};

}  // namespace

absl::StatusOr<std::unique_ptr<LlamaEngine>> CreateLlamaEngine(
    const std::string& model_path, int gpu_layers, int n_ctx) {
  llama_log_set(LlamaLogger, nullptr);
  llama_model_params model_params = llama_model_default_params();
  model_params.n_gpu_layers = gpu_layers;
  model_params.use_mmap = false;

  LOG(INFO) << "Loading LLM from " << model_path;
  LOG(INFO) << "GPU layers: " << gpu_layers;
  llama_model* model =
      llama_model_load_from_file(model_path.c_str(), model_params);
  if (!model) {
    return absl::InternalError(
        absl::StrCat("Failed to load model from ", model_path));
  }

  // If n_ctx not specified, read the model's training context size.
  if (n_ctx <= 0) {
    n_ctx = llama_model_n_ctx_train(model);
    LOG(INFO) << "Using model training context size: " << n_ctx;
  } else {
    LOG(INFO) << "Using configured context size: " << n_ctx;
  }

  const llama_vocab* vocab = llama_model_get_vocab(model);
  return std::make_unique<LlamaEngineImpl>(model, vocab, n_ctx);
}

}  // namespace ztab
