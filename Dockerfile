# SecLLM — control plane on top of the official vLLM image (vLLM CLI + CUDA already present).
# Build on a machine that can pull the (large) CUDA base; run with the NVIDIA runtime.
ARG VLLM_TAG=latest
FROM vllm/vllm-openai:${VLLM_TAG}

# uv for a fast, reproducible install of the control-plane deps (fastapi/uvicorn/httpx).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    SECLLM_HOST=0.0.0.0 \
    SECLLM_PORT=11400 \
    SECLLM_DATA_DIR=/var/lib/secllm \
    SECLLM_BACKEND=vllm

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
# vLLM is already in the base image; install SecLLM + its lightweight deps.
RUN uv pip install --system --no-cache . && mkdir -p /var/lib/secllm

EXPOSE 11400
VOLUME ["/var/lib/secllm"]

# Override the base image's vLLM entrypoint — SecLLM manages vLLM itself.
ENTRYPOINT ["secllm"]
