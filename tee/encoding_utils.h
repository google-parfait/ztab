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

#ifndef ZTAB_ENCODING_UTILS_H_
#define ZTAB_ENCODING_UTILS_H_

#include <string>

#include "absl/strings/escaping.h"

namespace ztab {

// Base64Url encoder (no padding), per RFC 4648 §5.
// Uses absl::WebSafeBase64Escape which produces the URL-safe alphabet,
// then strips trailing '=' padding to match JWT/JWS conventions.
inline std::string Base64UrlEncode(const std::string& input) {
  std::string encoded;
  absl::WebSafeBase64Escape(input, &encoded);
  // Strip trailing padding — JWT tokens use unpadded Base64Url.
  while (!encoded.empty() && encoded.back() == '=') {
    encoded.pop_back();
  }
  return encoded;
}

}  // namespace ztab

#endif  // ZTAB_ENCODING_UTILS_H_
