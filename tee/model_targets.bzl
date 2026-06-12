"""Macros for generating per-model ZTAB server OCI image targets."""

load("@rules_oci//oci:defs.bzl", "oci_image", "oci_load")
load("@rules_pkg//pkg:tar.bzl", "pkg_tar")
load(":build_defs.bzl", "define_load_runner")

def ztab_model_targets(model_name, weights_label, weights_filename):
    """Generates model_layer, oci_image, oci_load, and load_runner targets for a model.

    Creates targets for 2 deployment variants:
      - local: mock attestation, gpu_layers=0 (for development/testing)
      - gcp:   ITA attestation, gpu_layers=999 (for GCP Confidential Space)

    Args:
        model_name: Short model identifier (e.g., "gemma4_e4b"). Used in target names.
        weights_label: Bazel label for the GGUF weights file
            (e.g., "@gemma4_e4b_weights//:gemma-4-E4B-it-Q4_K_M.gguf").
        weights_filename: Filename of the GGUF file as it appears in /model/
            (e.g., "gemma-4-E4B-it-Q4_K_M.gguf").
    """

    model_layer_name = "model_layer_" + model_name

    pkg_tar(
        name = model_layer_name,
        srcs = [weights_label],
        mode = "0444",
        package_dir = "/model",
    )

    _VARIANTS = [
        {
            "suffix": "local",
            "cmd_extra": ["--attestation_provider=mock", "--gpu_layers=0"],
            "env": {},
        },
        {
            "suffix": "gcp",
            "cmd_extra": ["--attestation_provider=ita", "--gpu_layers=999"],
            "env": {"LD_LIBRARY_PATH": "/usr/local/nvidia/lib64:/usr/local/nvidia/lib"},
        },
    ]

    for variant in _VARIANTS:
        suffix = variant["suffix"]
        image_name = "ztab_server_{suffix}_{model}_oci_image".format(
            model = model_name,
            suffix = suffix,
        )
        tarball_name = "ztab_server_{suffix}_{model}_tarball".format(
            model = model_name,
            suffix = suffix,
        )
        repo_tag = "ztab-server-{suffix}-{model}:latest".format(
            model = model_name,
            suffix = suffix,
        )

        oci_image(
            name = image_name,
            base = "@distroless_cc_debian12_base",
            cmd = variant["cmd_extra"] + [
                "--model_path=/model/" + weights_filename,
                "--port=8000",
            ],
            entrypoint = ["/ztab_server"],
            env = variant["env"],
            exposed_ports = ["8000/tcp"],
            tars = [
                ":ztab_server_tar",
                ":" + model_layer_name,
            ],
        )

        oci_load(
            name = tarball_name,
            image = ":" + image_name,
            repo_tags = [repo_tag],
        )

        define_load_runner(
            "ztab_server_{suffix}_{model}".format(
                model = model_name,
                suffix = suffix,
            ),
            repo_tag,
        )
