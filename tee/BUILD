load("@com_github_grpc_grpc//bazel:cc_grpc_library.bzl", "cc_grpc_library")
load("@com_google_protobuf//bazel:cc_proto_library.bzl", "cc_proto_library")
load("@rules_cc//cc:defs.bzl", "cc_binary", "cc_library")
load("@rules_pkg//pkg:tar.bzl", "pkg_tar")
load("@rules_proto//proto:defs.bzl", "proto_library")
load("@bazel_skylib//rules:common_settings.bzl", "bool_flag")

package(default_visibility = ["//visibility:public"])

bool_flag(
    name = "enable_cuda",
    build_setting_default = False,
)

config_setting(
    name = "cuda_enabled",
    flag_values = {":enable_cuda": "True"},
)


# Proto compilation
proto_library(
    name = "session_manager_proto",
    srcs = ["session_manager.proto"],
    strip_import_prefix = "",
)

cc_proto_library(
    name = "session_manager_cc_proto",
    deps = [":session_manager_proto"],
)

cc_grpc_library(
    name = "session_manager_cc_grpc",
    srcs = [":session_manager_proto"],
    grpc_only = True,
    deps = [":session_manager_cc_proto"],
)

# TLS Certificate Generator Library
cc_library(
    name = "tls_cert_generator",
    srcs = ["tls_cert_generator.cc"],
    hdrs = ["tls_cert_generator.h"],
    deps = [
        "@boringssl//:crypto",
        "@boringssl//:ssl",
        "@com_google_absl//absl/status",
        "@com_google_absl//absl/status:statusor",
        "@com_google_absl//absl/strings",
    ],
)

# Attestation Token Provider Interface (header-only)
cc_library(
    name = "attestation_token_provider",
    hdrs = ["attestation_token_provider.h"],
    deps = [
        "@com_google_absl//absl/status:statusor",
        "@com_google_absl//absl/strings",
    ],
)

# Base64Url encoding utility (header-only)
cc_library(
    name = "encoding_utils",
    hdrs = ["encoding_utils.h"],
)

# Mock Attestation Token Provider (local development)
cc_library(
    name = "mock_attestation_token_provider",
    srcs = ["mock_attestation_token_provider.cc"],
    hdrs = ["mock_attestation_token_provider.h"],
    deps = [
        ":attestation_token_provider",
        ":encoding_utils",
        "@com_google_absl//absl/status:statusor",
        "@com_google_absl//absl/strings",
    ],
)

# HTTP Client (libcurl, for GCP attestation agent communication)
cc_library(
    name = "http_client",
    srcs = ["http_client.cc"],
    hdrs = ["http_client.h"],
    deps = [
        "@curl",
        "@com_google_absl//absl/status",
        "@com_google_absl//absl/status:statusor",
        "@com_google_absl//absl/strings",
    ],
)

# GCP Attestation Token Provider (Confidential Space / ITA)
cc_library(
    name = "gcp_attestation_token_provider",
    srcs = ["gcp_attestation_token_provider.cc"],
    hdrs = ["gcp_attestation_token_provider.h"],
    deps = [
        ":attestation_token_provider",
        ":http_client",
        "@com_google_absl//absl/log",
        "@com_google_absl//absl/status",
        "@com_google_absl//absl/status:statusor",
        "@com_google_absl//absl/strings",
    ],
)

# LLM Engine (llama.cpp wrapper)
cc_library(
    name = "llama_engine",
    srcs = ["llama_engine.cc"],
    hdrs = ["llama_engine.h"],
    deps = [
        "@llama_cpp//:llama_cpp",
        "@com_google_absl//absl/log",
        "@com_google_absl//absl/status",
        "@com_google_absl//absl/status:statusor",
        "@com_google_absl//absl/strings",
        "@com_google_absl//absl/strings:str_format",
        "@com_google_absl//absl/time",
    ],
)

# TLS Proxy Library
cc_library(
    name = "tls_proxy",
    srcs = ["tls_proxy.cc"],
    hdrs = ["tls_proxy.h"],
    deps = [
        ":attestation_token_provider",
        ":encoding_utils",
        ":tls_cert_generator",
        "@boringssl//:crypto",
        "@boringssl//:ssl",
        "@com_google_absl//absl/log",
        "@com_google_absl//absl/status",
        "@com_google_absl//absl/time",
    ],
)

# Policy Registry (dynamic policy loading from JSON files)
cc_library(
    name = "policy_registry",
    srcs = ["policy_registry.cc"],
    hdrs = ["policy_registry.h"],
    deps = [
        "@nlohmann_json//:json",
        "@com_google_absl//absl/container:flat_hash_map",
        "@com_google_absl//absl/log",
        "@com_google_absl//absl/status",
        "@com_google_absl//absl/status:statusor",
        "@com_google_absl//absl/strings",
    ],
)

# Session Manager (multi-agent session state machine)
cc_library(
    name = "session_manager",
    srcs = ["session_manager.cc"],
    hdrs = ["session_manager.h"],
    deps = [
        ":llama_engine",
        ":policy_registry",
        ":session_manager_cc_proto",
        ":session_manager_cc_grpc",
        "@boringssl//:crypto",
        "@nlohmann_json//:json",
        "@re2//:re2",
        "@com_google_absl//absl/container:flat_hash_map",
        "@com_google_absl//absl/log",
        "@com_google_absl//absl/status",
        "@com_google_absl//absl/status:statusor",
        "@com_google_absl//absl/strings",
        "@com_google_absl//absl/time",
    ],
)

# Server Binary
cc_binary(
    name = "ztab_server",
    srcs = ["main.cc"],
    deps = [
        ":attestation_token_provider",
        ":gcp_attestation_token_provider",
        ":llama_engine",
        ":mock_attestation_token_provider",
        ":policy_registry",
        ":session_manager",
        ":session_manager_cc_grpc",
        ":session_manager_cc_proto",
        ":tls_proxy",
        "@com_github_grpc_grpc//:grpc++",
        "@com_google_absl//absl/flags:flag",
        "@com_google_absl//absl/flags:parse",
        "@com_google_absl//absl/log",
        "@com_google_absl//absl/log:initialize",
        "@com_google_absl//absl/status",
        "@com_google_absl//absl/status:statusor",
        "@com_google_absl//absl/strings",
        "@com_google_absl//absl/time",
    ],
)

# --- Binary Packaging (used by //gcp:BUILD for OCI image assembly) ---

# Binary layer: packages ztab_server into a tar for OCI image layering.
pkg_tar(
    name = "ztab_server_tar",
    srcs = [":ztab_server"],
    include_runfiles = True,
)
