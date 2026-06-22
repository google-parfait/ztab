# test/harness/bin/

This directory is for staging the pre-built Language Server binary before
building the Docker image.

## How to stage

Copy the compiled Language Server binary here before running `docker build`:

```bash
cp /path/to/language_server test/harness/bin/
```

The Dockerfile expects the binary at `test/harness/bin/language_server`.

## .gitignore

The binary itself is gitignored. Only this README is tracked.
