"""GCP deployment targets for the ZTAB server.

This package handles OCI image assembly by combining the C++ server
binary from //tee with policy files from //examples. The tee/ package
builds the server; this package bundles it for deployment.
"""

load("@rules_oci//oci:defs.bzl", "oci_image", "oci_load")
load("@rules_pkg//pkg:tar.bzl", "pkg_tar")
load(":model_targets.bzl", "ztab_model_targets")

package(default_visibility = ["//visibility:public"])

# Policy layer: packages JSON policy files into /policies/ for OCI image.
pkg_tar(
    name = "policy_layer",
    srcs = ["//examples/calendar:policy.json"],
    mode = "0444",
    package_dir = "/policies",
)

# Echo-only image (no model weights — for quick testing).
oci_image(
    name = "ztab_server_echo_oci_image",
    base = "@distroless_cc_debian12_base",
    cmd = [
        "--attestation_provider=mock",
        "--policy_dir=/policies",
        "--port=8000",
    ],
    entrypoint = ["/ztab_server"],
    exposed_ports = ["8000/tcp"],
    tars = [
        "//tee:ztab_server_tar",
        ":policy_layer",
    ],
)

oci_load(
    name = "ztab_server_echo_tarball",
    image = ":ztab_server_echo_oci_image",
    repo_tags = ["ztab-server-echo:latest"],
)

# Per-model GCP server container targets.
# Each call generates: model_layer_{name}, oci_image, oci_load, and load_runner
# for 2 deployment variants (local, gcp).
ztab_model_targets(
    model_name = "gemma4_e2b",
    weights_filename = "gemma-4-E2B_q4_0-it.gguf",
    weights_label = "@gemma4_e2b_weights//:gemma-4-E2B_q4_0-it.gguf",
)

ztab_model_targets(
    model_name = "gemma4_e4b",
    weights_filename = "gemma-4-E4B-it-Q4_K_M.gguf",
    weights_label = "@gemma4_e4b_weights//:gemma-4-E4B-it-Q4_K_M.gguf",
)

ztab_model_targets(
    model_name = "gemma4_31b",
    weights_filename = "gemma-4-31B-it-Q4_K_M.gguf",
    weights_label = "@gemma4_31b_weights//:gemma-4-31B-it-Q4_K_M.gguf",
)
