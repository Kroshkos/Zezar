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
        wget curl ca-certificates sudo apt-utils git jq tmux build-essential ripgrep ffmpeg procps\
        libgl1-mesa-glx \
        libasound2-plugins libatomic1 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*




# 设置工作目录
WORKDIR /app

# 复制项目代码
COPY . .


# 安装 uv 并切换到国内源
RUN mkdir -p ./openhands/bin && \
    # 使用国内源加速下载
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="./openhands/bin" sh

# 安装 micromamba 并配置国内源
# 安装 micromamba 到本地目录
RUN curl -Ls https://micro.mamba.pm/install.sh | bash -s -- -b -p ./openhands/micromamba && \
    ./openhands/micromamba/bin/micromamba config add channels https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main && \
    ./openhands/micromamba/bin/micromamba config add channels https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/free && \
    ./openhands/micromamba/bin/micromamba config add channels https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge && \
    ./openhands/micromamba/bin/micromamba config set show_channel_urls yes && \
    ./openhands/micromamba/bin/micromamba config remove channels defaults && \
    ./openhands/micromamba/bin/micromamba config list



# 创建环境并安装依赖
RUN ./openhands/micromamba/bin/micromamba create -n openhands python=3.9 -y && \
    ./openhands/micromamba/bin/micromamba clean -ay

# 安装 playwright 并使用国内源
RUN apt-get update && \
    # 使用国内 PyPI 镜像加速 pip 安装
    ./openhands/micromamba/bin/micromamba run -n openhands pip install -i https://pypi.tuna.tsinghua.edu.cn/simple playwright && \
    # 使用国内镜像安装 playwright 浏览器
    ./openhands/micromamba/bin/micromamba run -n openhands playwright install --with-deps chromium && \
    rm -rf /var/lib/apt/lists/*

# 安装 openvscode-server 并使用国内源
#ARG RELEASE_TAG="openvscode-server-v1.98.2"
#ARG RELEASE_ORG="gitpod-io"
#RUN arch=$(uname -m) && \
#    # 使用国内镜像加速下载
#    wget -c https://ghproxy.com/https://github.com/${RELEASE_ORG}/openvscode-server/releases/download/${RELEASE_TAG}/${RELEASE_TAG}-linux-${arch}.tar.gz -O ${RELEASE_TAG}-linux-${arch}.tar.gz && \
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