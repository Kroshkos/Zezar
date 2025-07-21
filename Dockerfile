FROM hub.docker.alibaba-inc.com/aone-base-global/python:312

# 设置环境变量
ENV POETRY_VIRTUALENVS_PATH=./openhands/poetry \
    MAMBA_ROOT_PREFIX=./openhands/micromamba \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    EDITOR=code \
    VISUAL=code \
    GIT_EDITOR="code --wait" \
    OPENVSCODE_SERVER_ROOT=./openhands/.openvscode-server

# 安装依赖
# 使用国内源更新和安装依赖
# 替换为国内源并安装系统依赖
# 替换为国内源并安装系统依赖
RUN rm -rf /etc/apt/sources.list && \
    rm -rf /etc/apt/sources.list.d/* && \
    echo "deb http://mirrors.aliyun.com/debian bookworm main contrib non-free" > /etc/apt/sources.list && \
    echo "deb http://mirrors.aliyun.com/debian-security bookworm-security main contrib non-free" >> /etc/apt/sources.list && \
    echo "deb http://mirrors.aliyun.com/debian bookworm-updates main contrib non-free" >> /etc/apt/sources.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        wget curl ca-certificates sudo apt-utils git jq tmux build-essential ripgrep ffmpeg \
        libgl1-mesa-glx \
        libasound2-plugins libatomic1 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*




# 设置工作目录
WORKDIR /app

# 复制项目代码
COPY . .


# 安装uv工具
#RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="./openhands/bin" sh
#
## 安装micromamba
#RUN mkdir -p ./openhands/micromamba/bin && \
#    /bin/bash -c "PREFIX_LOCATION=./openhands/micromamba BIN_FOLDER=./openhands/micromamba/bin INIT_YES=no CONDA_FORGE_YES=yes $(curl -L https://micro.mamba.pm/install.sh)" && \
#    ./openhands/micromamba/bin/micromamba config remove channels defaults && \
#    ./openhands/micromamba/bin/micromamba config list
#
## 安装playwright
#RUN apt-get update && \
#    ./openhands/micromamba/bin/micromamba run -n openhands poetry run pip install playwright && \
#    ./openhands/micromamba/bin/micromamba run -n openhands poetry run playwright install --with-deps chromium

# 安装openvscode-server
#ARG RELEASE_TAG="openvscode-server-v1.98.2"
#ARG RELEASE_ORG="gitpod-io"
#RUN arch=$(uname -m) && \
#    wget https://github.com/${RELEASE_ORG}/openvscode-server/releases/download/${RELEASE_TAG}/${RELEASE_TAG}-linux-${arch}.tar.gz && \
#    tar -xzf ${RELEASE_TAG}-linux-${arch}.tar.gz && \
#    mv ${RELEASE_TAG}-linux-${arch} ${OPENVSCODE_SERVER_ROOT} && \
#    cp ${OPENVSCODE_SERVER_ROOT}/bin/remote-cli/openvscode-server ${OPENVSCODE_SERVER_ROOT}/bin/remote-cli/code && \
#    rm -f ${RELEASE_TAG}-linux-${arch}.tar.gz

# 安装依赖（放在复制代码前，提高缓存利用率）
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple/

# 暴露端口
EXPOSE 35123

# 启动命令（调整路径，直接使用工作目录）
CMD ["python", "openhands/action_execution_server.py", "35123", "--working-dir", "/workspace"]