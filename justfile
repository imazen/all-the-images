# all-the-images justfile

# Build the Docker image
build:
    docker compose build

# Generate the full corpus
generate:
    docker compose run --rm generate

# Generate quick smoke-test subset
quick:
    docker compose run --rm quick

# Interactive shell with all encoders
shell:
    docker compose run --rm shell

# Local smoke test (no Docker, uses system encoders)
smoke-test:
    #!/usr/bin/env bash
    set -euo pipefail
    export CJPEG_TURBO="${CJPEG_TURBO:-/usr/bin/cjpeg}"
    export DJPEG_TURBO="${DJPEG_TURBO:-/usr/bin/djpeg}"
    export CJPEGLI="${CJPEGLI:-/usr/local/bin/cjpegli}"
    python3 scripts/generate.py --output /tmp/ati-smoke --quick --skip-reference
    echo "Smoke test passed. Output: /tmp/ati-smoke/"

# Validate manifest schema
validate-schema:
    python3 -c "import json; json.load(open('manifest/schema.json')); print('Schema: valid')"

# Show encoder versions inside Docker
versions:
    docker compose run --rm shell -c '\
        echo "=== IJG ===" && $CJPEG_IJG -version 2>&1 | head -1; \
        echo "=== turbo ===" && $CJPEG_TURBO -version 2>&1 | head -1; \
        echo "=== mozjpeg ===" && $CJPEG_MOZ -version 2>&1 | head -1; \
        echo "=== jpegli ===" && $CJPEGLI --version 2>&1 | head -1; \
        echo "=== guetzli ===" && $GUETZLI --help 2>&1 | head -1'

# Clean generated corpus
clean:
    rm -rf corpus/

# List corpus stats from manifest
stats:
    python3 -c "\
    import json; \
    m=json.load(open('corpus/manifest.json')); \
    s=m['stats']; \
    print(f'Files: {s[\"total_files\"]}'); \
    print(f'Unique: {s[\"unique_hashes\"]}'); \
    print(f'Size: {s[\"total_bytes\"]/1024/1024:.1f} MB'); \
    print(f'Encoders: {s[\"encoders_used\"]}'); \
    print(f'Sources: {s[\"sources_used\"]}'); \
    print(f'Failures: {s[\"encoding_failures\"]}')"
