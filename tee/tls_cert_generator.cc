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

#include "tls_cert_generator.h"

#include <openssl/asn1.h>
#include <openssl/ec.h>
#include <openssl/evp.h>
#include <openssl/obj.h>
#include <openssl/pem.h>
#include <openssl/sha.h>
#include <openssl/x509.h>
#include <openssl/x509v3.h>

#include <memory>
#include <string>
#include <utility>

#include "absl/status/status.h"
#include "absl/status/statusor.h"
#include "absl/strings/str_cat.h"

namespace ztab {
namespace {

// Custom OID for the attestation extension.
// In production this should be a properly registered OID; this placeholder is
// under the "private enterprise" arc and won't collide with anything real.
constexpr char kAttestationOid[] = "1.3.6.1.4.1.99999.1";

using X509_ptr = std::unique_ptr<
    X509, EphemeralCredentialGenerator::OpenSSLDeleter<X509, X509_free>>;
struct BIODeleter {
  void operator()(BIO* bio) const {
    if (bio != nullptr) BIO_free(bio);
  }
};
using BIO_ptr = std::unique_ptr<BIO, BIODeleter>;
using ASN1_OBJECT_ptr =
    std::unique_ptr<ASN1_OBJECT, EphemeralCredentialGenerator::OpenSSLDeleter<
                                     ASN1_OBJECT, ASN1_OBJECT_free>>;
using ASN1_OCTET_STRING_ptr =
    std::unique_ptr<ASN1_OCTET_STRING,
                    EphemeralCredentialGenerator::OpenSSLDeleter<
                        ASN1_OCTET_STRING, ASN1_OCTET_STRING_free>>;
using X509_EXTENSION_ptr =
    std::unique_ptr<X509_EXTENSION,
                    EphemeralCredentialGenerator::OpenSSLDeleter<
                        X509_EXTENSION, X509_EXTENSION_free>>;

}  // namespace

EphemeralCredentialGenerator::EphemeralCredentialGenerator() : pkey_(nullptr) {}
EphemeralCredentialGenerator::~EphemeralCredentialGenerator() = default;

absl::StatusOr<std::string>
EphemeralCredentialGenerator::GenerateKeyAndGetHash() {
  // Generate an EC Key Pair (NIST P-256 / secp256r1).
  EVP_PKEY_CTX* pkctx = EVP_PKEY_CTX_new_id(EVP_PKEY_EC, nullptr);
  if (pkctx == nullptr) {
    return absl::InternalError("Failed to create key generator context.");
  }
  std::unique_ptr<EVP_PKEY_CTX, void (*)(EVP_PKEY_CTX*)> pkctx_guard(
      pkctx, EVP_PKEY_CTX_free);

  if (EVP_PKEY_keygen_init(pkctx) <= 0) {
    return absl::InternalError("Failed to initialize key generator.");
  }
  if (EVP_PKEY_CTX_set_ec_paramgen_curve_nid(pkctx, NID_X9_62_prime256v1) <=
      0) {
    return absl::InternalError("Failed to set curve SECP256R1.");
  }
  EVP_PKEY* raw_pkey = nullptr;
  if (EVP_PKEY_keygen(pkctx, &raw_pkey) <= 0) {
    return absl::InternalError("Failed to generate EC key pair.");
  }
  pkey_.reset(raw_pkey);

  // Extract public key in SubjectPublicKeyInfo DER format and SHA-256 hash it.
  unsigned char* pub_der = nullptr;
  int der_len = i2d_PUBKEY(pkey_.get(), &pub_der);
  if (der_len <= 0) {
    return absl::InternalError("Failed to serialize public key to DER.");
  }
  std::unique_ptr<unsigned char, void (*)(void*)> pub_der_guard(pub_der,
                                                                OPENSSL_free);

  unsigned char hash[SHA256_DIGEST_LENGTH];
  SHA256(pub_der, der_len, hash);

  return std::string(reinterpret_cast<char*>(hash), SHA256_DIGEST_LENGTH);
}

absl::StatusOr<std::pair<std::string, std::string>>
EphemeralCredentialGenerator::GenerateCertificate(
    const std::string& attestation_token) {
  if (!pkey_) {
    return absl::FailedPreconditionError(
        "Key pair not generated. Call GenerateKeyAndGetHash() first.");
  }

  // Create self-signed X.509v3 certificate.
  X509_ptr x509(X509_new());
  if (!x509) {
    return absl::InternalError("Failed to create X509 structure.");
  }

  if (X509_set_version(x509.get(), 2) <= 0) {  // v3
    return absl::InternalError("Failed to set certificate version.");
  }
  ASN1_INTEGER_set(X509_get_serialNumber(x509.get()), 1);

  // Valid for 1 day.
  X509_gmtime_adj(X509_get_notBefore(x509.get()), 0);
  X509_gmtime_adj(X509_get_notAfter(x509.get()), 24 * 3600);

  if (X509_set_pubkey(x509.get(), pkey_.get()) <= 0) {
    return absl::InternalError(
        "Failed to associate public key with certificate.");
  }

  // Subject and issuer: self-signed, CN=ztab-tee.
  X509_NAME* name = X509_get_subject_name(x509.get());
  if (name == nullptr) {
    return absl::InternalError("Failed to get subject name structure.");
  }
  if (X509_NAME_add_entry_by_txt(
          name, "CN", MBSTRING_ASC,
          reinterpret_cast<const unsigned char*>("ztab-tee"), -1, -1, 0) <= 0) {
    return absl::InternalError("Failed to set subject CN.");
  }
  if (X509_set_issuer_name(x509.get(), name) <= 0) {
    return absl::InternalError("Failed to set issuer name.");
  }

  // Add custom extension carrying the attestation token.
  ASN1_OBJECT_ptr obj(OBJ_txt2obj(kAttestationOid, 1));
  if (!obj) {
    return absl::InternalError(
        absl::StrCat("Failed to create ASN1_OBJECT for OID ", kAttestationOid));
  }

  ASN1_OCTET_STRING_ptr octet_str(ASN1_OCTET_STRING_new());
  if (!octet_str) {
    return absl::InternalError("Failed to create ASN1_OCTET_STRING.");
  }
  if (ASN1_OCTET_STRING_set(
          octet_str.get(),
          reinterpret_cast<const unsigned char*>(attestation_token.data()),
          attestation_token.size()) <= 0) {
    return absl::InternalError("Failed to set value for ASN1_OCTET_STRING.");
  }

  // DER-encode the OCTET STRING to set as raw extension value.
  unsigned char* der_buf = nullptr;
  int der_len = i2d_ASN1_OCTET_STRING(octet_str.get(), &der_buf);
  if (der_len <= 0) {
    return absl::InternalError("Failed to DER encode ASN1_OCTET_STRING.");
  }
  std::unique_ptr<unsigned char, void (*)(void*)> der_buf_guard(der_buf,
                                                                OPENSSL_free);

  ASN1_OCTET_STRING_ptr ext_value(ASN1_OCTET_STRING_new());
  if (!ext_value) {
    return absl::InternalError("Failed to create extension value container.");
  }
  if (ASN1_OCTET_STRING_set(ext_value.get(), der_buf, der_len) <= 0) {
    return absl::InternalError("Failed to set extension value bytes.");
  }

  X509_EXTENSION_ptr ext(X509_EXTENSION_create_by_OBJ(
      nullptr, obj.get(), /*critical=*/0, ext_value.get()));
  if (!ext) {
    return absl::InternalError("Failed to create X509_EXTENSION.");
  }
  if (X509_add_ext(x509.get(), ext.get(), -1) <= 0) {
    return absl::InternalError("Failed to add extension to certificate.");
  }

  // Sign the certificate with the ephemeral private key.
  if (X509_sign(x509.get(), pkey_.get(), EVP_sha256()) <= 0) {
    return absl::InternalError("Failed to sign certificate.");
  }

  // Export private key to PEM.
  BIO_ptr bio_key(BIO_new(BIO_s_mem()));
  if (!bio_key) {
    return absl::InternalError("Failed to create BIO for private key.");
  }
  if (PEM_write_bio_PrivateKey(bio_key.get(), pkey_.get(), nullptr, nullptr, 0,
                               nullptr, nullptr) <= 0) {
    return absl::InternalError("Failed to write private key to PEM.");
  }
  char* key_data = nullptr;
  long key_len = BIO_get_mem_data(bio_key.get(), &key_data);
  std::string private_key_pem(key_data, key_len);

  // Export certificate to PEM.
  BIO_ptr bio_cert(BIO_new(BIO_s_mem()));
  if (!bio_cert) {
    return absl::InternalError("Failed to create BIO for certificate.");
  }
  if (PEM_write_bio_X509(bio_cert.get(), x509.get()) <= 0) {
    return absl::InternalError("Failed to write certificate to PEM.");
  }
  char* cert_data = nullptr;
  long cert_len = BIO_get_mem_data(bio_cert.get(), &cert_data);
  std::string cert_pem(cert_data, cert_len);

  return std::make_pair(cert_pem, private_key_pem);
}

}  // namespace ztab
