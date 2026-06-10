load("@com_github_grpc_grpc//bazel:cc_grpc_library.bzl", "cc_grpc_library")
load("@com_google_protobuf//bazel:cc_proto_library.bzl", "cc_proto_library")
load("@rules_cc//cc:defs.bzl", "cc_binary", "cc_library")
load("@rules_proto//proto:defs.bzl", "proto_library")

package(default_visibility = ["//visibility:public"])

# Proto compilation
proto_library(
    name = "session_manager_proto",
    srcs = ["session_manager.proto"],
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

# Server Binary
cc_binary(
    name = "ztab_server",
    srcs = ["main.cc"],
    deps = [
        ":session_manager_cc_grpc",
        ":session_manager_cc_proto",
        ":tls_cert_generator",
        "@com_github_grpc_grpc//:grpc++",
        "@com_google_absl//absl/log",
        "@com_google_absl//absl/log:initialize",
        "@com_google_absl//absl/status:statusor",
        "@com_google_absl//absl/strings",
    ],
)
