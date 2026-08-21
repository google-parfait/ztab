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

#include "tls_cert_generator.h"

#include <string>
#include <utility>

#include "absl/status/status.h"
#include "absl/status/statusor.h"
#include "gtest/gtest.h"
#include "openssl/pem.h"
#include "openssl/x509.h"

namespace ztab {
namespace {

// --- Key Generation ---

TEST(TlsCertGeneratorTest, GenerateKeyReturns32ByteHash) {
  EphemeralCredentialGenerator gen;
  auto hash_or = gen.GenerateKeyAndGetHash();
  ASSERT_TRUE(hash_or.ok()) << hash_or.status();
  // SHA-256 hash is exactly 32 bytes.
  EXPECT_EQ(hash_or->size(), 32);
}

TEST(TlsCertGeneratorTest, TwoGeneratorsProduceDifferentKeys) {
  EphemeralCredentialGenerator gen1;
  EphemeralCredentialGenerator gen2;
  auto h1 = gen1.GenerateKeyAndGetHash();
  auto h2 = gen2.GenerateKeyAndGetHash();
  ASSERT_TRUE(h1.ok());
  ASSERT_TRUE(h2.ok());
  EXPECT_NE(*h1, *h2);
}

// --- Certificate Generation ---

TEST(TlsCertGeneratorTest, GenerateCertificateFailsBeforeKeyGeneration) {
  EphemeralCredentialGenerator gen;
  auto result = gen.GenerateCertificate("some-token");
  ASSERT_FALSE(result.ok());
  EXPECT_EQ(result.status().code(), absl::StatusCode::kFailedPrecondition);
}

TEST(TlsCertGeneratorTest, GenerateCertificateReturnsValidPem) {
  EphemeralCredentialGenerator gen;
  auto hash_or = gen.GenerateKeyAndGetHash();
  ASSERT_TRUE(hash_or.ok());

  auto cert_or = gen.GenerateCertificate("test-attestation-token");
  ASSERT_TRUE(cert_or.ok()) << cert_or.status();

  const auto& [cert_pem, key_pem] = *cert_or;

  // Verify cert PEM is parseable.
  EXPECT_TRUE(cert_pem.find("BEGIN CERTIFICATE") != std::string::npos);
  EXPECT_TRUE(key_pem.find("BEGIN") != std::string::npos);

  // Parse the cert and verify subject CN.
  BIO* bio = BIO_new_mem_buf(cert_pem.data(), cert_pem.size());
  ASSERT_NE(bio, nullptr);
  X509* x509 = PEM_read_bio_X509(bio, nullptr, nullptr, nullptr);
  BIO_free(bio);
  ASSERT_NE(x509, nullptr);

  X509_NAME* subject = X509_get_subject_name(x509);
  char cn[256] = {};
  X509_NAME_get_text_by_NID(subject, NID_commonName, cn, sizeof(cn));
  EXPECT_STREQ(cn, "ztab-tee");

  // Verify it's X.509 v3.
  EXPECT_EQ(X509_get_version(x509), 2);  // v3 = version 2

  X509_free(x509);
}

TEST(TlsCertGeneratorTest, CertificateContainsAttestationOid) {
  EphemeralCredentialGenerator gen;
  auto hash_or = gen.GenerateKeyAndGetHash();
  ASSERT_TRUE(hash_or.ok());

  const std::string token = "my-test-jwt-token";
  auto cert_or = gen.GenerateCertificate(token);
  ASSERT_TRUE(cert_or.ok());

  const auto& [cert_pem, _] = *cert_or;

  // Parse cert.
  BIO* bio = BIO_new_mem_buf(cert_pem.data(), cert_pem.size());
  X509* x509 = PEM_read_bio_X509(bio, nullptr, nullptr, nullptr);
  BIO_free(bio);
  ASSERT_NE(x509, nullptr);

  // Look for the custom OID extension.
  ASN1_OBJECT* oid = OBJ_txt2obj("1.3.6.1.4.1.99999.1", 1);
  ASSERT_NE(oid, nullptr);

  int ext_idx = X509_get_ext_by_OBJ(x509, oid, -1);
  EXPECT_GE(ext_idx, 0) << "Attestation OID extension not found";

  if (ext_idx >= 0) {
    X509_EXTENSION* ext = X509_get_ext(x509, ext_idx);
    ASSERT_NE(ext, nullptr);

    // The extension value is DER-encoded OCTET STRING containing
    // another OCTET STRING with the token bytes. Verify the token
    // is embedded somewhere in the raw extension data.
    const ASN1_OCTET_STRING* data = X509_EXTENSION_get_data(ext);
    std::string raw(reinterpret_cast<const char*>(ASN1_STRING_get0_data(data)),
                    ASN1_STRING_length(data));
    EXPECT_NE(raw.find(token), std::string::npos)
        << "Token not found in extension data";
  }

  ASN1_OBJECT_free(oid);
  X509_free(x509);
}

}  // namespace
}  // namespace ztab
