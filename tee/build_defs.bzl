"""Build definitions for the ZTAB server OCI images."""

def define_load_runner(variant, image_and_tag, name = None):
    native.genrule(
        name = "load_and_print_digest_runner_" + variant,
        outs = ["load_and_print_digest_" + variant + ".sh"],
        cmd = """
            set -e # Exit on error
            TARBALL_SCRIPT=$(location :{variant}_tarball)
            IMAGE_TAG="{image_and_tag}"

            # --- STEP 1: Execute the oci_load script ---
            echo "Executing oci_load script: $$TARBALL_SCRIPT" >&2
            $$TARBALL_SCRIPT # This runs 'docker load'
            echo "Image load complete." >&2

            # --- STEP 2: Get the Image ID for the loaded tag ---
            echo "Inspecting tag '$$IMAGE_TAG' to get Image ID..." >&2
            LOADED_ID=$$(docker image inspect "$$IMAGE_TAG" --format '{{{{.Id}}}}' 2>/dev/null)

            if [ -z "$$LOADED_ID" ]; then
                echo "ERROR: Could not inspect tag '$$IMAGE_TAG' to get Image ID after load." >&2
                exit 1
            fi
            echo "Found Image ID for tag '$$IMAGE_TAG': $$LOADED_ID" >&2

            # --- STEP 3: Inspect the specific Image ID to get the RepoDigest (Manifest Digest) ---
            echo "Inspecting loaded Image ID '$$LOADED_ID' for RepoDigest..." >&2
            
            DIGEST=$$(docker image inspect "$$LOADED_ID" --format '{{{{range .RepoDigests}}}}{{{{.}}}}{{{{println}}}}{{{{end}}}}' 2>/dev/null | grep -o 'sha256:[a-f0-9]*' | head -n 1 || true)

            # Fallback to the Image ID itself ONLY if RepoDigests is empty for that ID
            if [ -z "$$DIGEST" ]; then
                echo "WARNING: RepoDigest not found for specific Image ID $$LOADED_ID after load. Falling back to Image ID (Config Digest)." >&2
                # Use the ID we got from inspecting the tag
                DIGEST="$$LOADED_ID"
            else
                echo "Found RepoDigest (Manifest Digest) for Image ID $$LOADED_ID: $$DIGEST" >&2
            fi

            # --- STEP 4: Print the final digest ---
            echo "==================================================" >&2
            echo "Server Image ({image_and_tag}) Docker Digest: $$DIGEST" >&2
            echo "==================================================" >&2

            # --- STEP 5: Generate the dummy output script for bazel run ---
            printf '#!/bin/bash\\n echo "Wrapper script finished."\\n exit 0\\n' > $@
            chmod +x $@
        """.format(variant = variant, image_and_tag = image_and_tag),
        executable = True,  # Make runnable via `bazel run`
        local = True,  # Allow access to local Docker daemon
        tools = [":" + variant + "_tarball"],
        visibility = ["//visibility:public"],
    )
