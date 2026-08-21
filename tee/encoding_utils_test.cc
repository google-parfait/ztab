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
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
// implied. See the License for the specific language governing
// permissions and limitations under the License.

#include "encoding_utils.h"

#include <string>

#include "gtest/gtest.h"

namespace ztab {
namespace {

TEST(EncodingUtilsTest, EmptyInput) { EXPECT_EQ(Base64UrlEncode(""), ""); }

TEST(EncodingUtilsTest, SingleByte) {
  // 'A' -> Base64 = 'QQ==' -> Base64Url no pad = 'QQ'
  std::string result = Base64UrlEncode("A");
  EXPECT_EQ(result, "QQ");
  // Must not contain padding.
  EXPECT_EQ(result.find('='), std::string::npos);
}

TEST(EncodingUtilsTest, ThreeBytes) {
  // 'ABC' -> Base64 = 'QUJD' (no padding needed for 3 bytes).
  EXPECT_EQ(Base64UrlEncode("ABC"), "QUJD");
}

TEST(EncodingUtilsTest, NoPaddingInOutput) {
  // Two bytes -> Base64 = 'QUI=' -> stripped = 'QUI'
  std::string result = Base64UrlEncode("AB");
  EXPECT_EQ(result.find('='), std::string::npos);
  EXPECT_EQ(result, "QUI");
}

TEST(EncodingUtilsTest, BinaryInput) {
  // Null bytes and binary data should work.
  std::string binary("\x00\x01\x02\x03", 4);
  std::string result = Base64UrlEncode(binary);
  EXPECT_FALSE(result.empty());
  EXPECT_EQ(result.find('='), std::string::npos);
}

TEST(EncodingUtilsTest, UrlSafeCharacters) {
  // Ensure + becomes - and / becomes _ (WebSafe alphabet).
  // Input that produces + and / in standard base64:
  // 0xFB, 0xFF, 0xFE -> standard base64 = "u//+" -> websafe = "u__-"
  std::string input("\xfb\xff\xfe", 3);
  std::string result = Base64UrlEncode(input);
  EXPECT_EQ(result.find('+'), std::string::npos)
      << "Output must not contain '+'";
  EXPECT_EQ(result.find('/'), std::string::npos)
      << "Output must not contain '/'";
}

}  // namespace
}  // namespace ztab
