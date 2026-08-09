# ComfyUI — node-graph diffusion UI, served as a web app.
#
# Built on PyTorch's own CUDA runtime image rather than a bare nvidia/cuda one.
# ComfyUI's requirements.txt would otherwise pull torch from PyPI, which means
# resolving a multi-gigabyte wheel whose CUDA build has to match the base image
# and the host driver. Starting from an image where torch is already correct
# removes the one dependency in this stack most likely to install "successfully"
# and then fail at runtime with a CUDA mismatch.
#
# The torch VERSION is not free to choose either. ComfyUI now pulls in
# comfy_kitchen, which registers custom ops annotated `list[int]`. torch 2.5's
# infer_schema rejects that type outright, so the image built perfectly and
# then died on import every single time — and because SM runs containers with
# --rm, the container erased itself before the traceback could be read. It
# presented as "launch succeeds, nothing runs, no logs anywhere". 2.8 accepts
# those annotations.
FROM pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# git: ComfyUI and its Manager are cloned, and the Manager also uses git at
# RUNTIME to install custom nodes on request — so it is a dependency of the
# running app, not just of this build.
# libgl1/libglib2.0-0: OpenCV's shared-library dependencies. Pulled in by
# several stock nodes; without them the import fails at model-load time rather
# than at startup, which reads as a broken workflow instead of a missing package.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git

WORKDIR /opt/ComfyUI
# --no-deps is NOT used: ComfyUI's requirements pin things the base image does
# not carry. torch itself is already satisfied by the base, so pip leaves it be.
RUN pip install --no-cache-dir -r requirements.txt

# ComfyUI-Manager is what makes the "press a button to download a model"
# workflow exist at all: model catalogue, custom-node installs, and the
# template/tutorial downloads. Without it ComfyUI can only load models that
# were already placed on disk by hand, which is precisely the workflow this
# app is meant to avoid.
RUN git clone --depth 1 https://github.com/ltdrdata/ComfyUI-Manager.git \
        custom_nodes/comfyui-manager \
 && pip install --no-cache-dir -r custom_nodes/comfyui-manager/requirements.txt

# The two directories worth keeping across a container recreate. Created here so
# the volumes mount onto real, correctly-owned paths instead of springing into
# existence as root-owned mountpoints on first launch.
RUN mkdir -p /opt/ComfyUI/models /opt/ComfyUI/output /opt/ComfyUI/input \
             /opt/ComfyUI/user

EXPOSE 8188

# --listen 0.0.0.0: reachable from outside the container, which is how the
#   Server Manager's proxy gets to it. It is never exposed to the network
#   directly — the Hub authenticates in front of it.
# --preview-method auto: node previews during sampling, so a long generation
#   shows progress instead of appearing frozen on a phone screen.
CMD ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188", \
     "--preview-method", "auto"]
