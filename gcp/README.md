# ZTAB GCP Deployment Scripts

Generic scripts for deploying the ZTAB TEE server to GCP Confidential Space.
These scripts have no hardcoded project-specific defaults — all deployment
parameters are provided via flags.

## Scripts

| Script | Purpose |
|:-------|:--------|
| `build_and_push.sh` | Build the ZTAB server OCI image (via Bazel) and push to Artifact Registry. |
| `launch.sh` | Manage Confidential Space VMs: setup templates, launch, get IP, delete. |

## Usage

### Build and push an image

```bash
gcp/build_and_push.sh \
    --ztab_dir $(pwd) \
    --image_base us-docker.pkg.dev/myproject/myrepo/ztab-server \
    --gcs_bucket gs://my-model-weights \
    --model gemma4_e4b
```

### Set up and launch a Confidential Space VM

```bash
# Create the VM template and MIG
gcp/launch.sh \
    --project myproject \
    --zone us-east5-a \
    --image_base us-docker.pkg.dev/myproject/myrepo/ztab-server \
    --ita_api_key YOUR_API_KEY \
    --mode setup

# Request a node
gcp/launch.sh \
    --project myproject \
    --zone us-east5-a \
    --mode launch

# Poll for the external IP
gcp/launch.sh \
    --project myproject \
    --zone us-east5-a \
    --mode get-ip

# Tear down
gcp/launch.sh \
    --project myproject \
    --zone us-east5-a \
    --mode delete
```

## For deployment-specific wrappers

Create a wrapper script that provides your project's defaults:

```bash
#!/bin/bash
ZTAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "${ZTAB_DIR}/gcp/launch.sh" \
    --project myproject \
    --zone us-east5-a \
    --image_base us-docker.pkg.dev/myproject/myrepo/ztab-server \
    "$@"
```
